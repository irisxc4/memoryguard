from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time

import pytest

from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2, V2ContextError, V2GovernanceError, V2MutationContext, V2ScopeError
from memoryguard.memory import MemoryAtom, MemoryAtomStore


def _ctx(root: Path, *, agent: str = "agent-a", project: str = "proj", authority: str = "manual", admin: bool = False) -> V2MutationContext:
    return V2MutationContext(
        workspace_id=str(root),
        share_group_id="group-a",
        agent_instance_id=agent,
        project_ref=project,
        provider="provider-a",
        runtime_role="runtime-a",
        actor=agent,
        authority=authority,
        admin=admin,
    )


def test_context_scope_blocks_cross_agent_group_and_auto_expansion(tmp_path: Path):
    boundary = GovernanceV2(tmp_path)
    ctx = _ctx(tmp_path, authority="auto")
    atom, decision = boundary.put_atom(
        MemoryAtom(memory_id="m1", body="fact", share_group_id="group-a", agent_instance_id="agent-a", project_ref="proj"),
        context=ctx,
        evidence=[{"source_ref": "legacy/group-a#m1", "digest": "d1"}],
        reason="automatic import",
    )
    assert atom.share_group_id == "group-a"
    assert decision.reason == "automatic import"
    with pytest.raises(V2ScopeError):
        boundary.put_atom(
            MemoryAtom(memory_id="m2", body="fact", share_group_id="group-b", agent_instance_id="agent-b"),
            context=ctx,
            evidence=[{"source_ref": "legacy/group-b#m2", "digest": "d2"}],
            reason="cross scope",
        )
    with pytest.raises(V2ScopeError):
        boundary.put_atom(
            MemoryAtom(memory_id="m3", body="fact", share_group_id="group-a", agent_instance_id="agent-b"),
            context=ctx,
            evidence=[{"source_ref": "legacy/group-a#m3", "digest": "d3"}],
            reason="cross agent",
        )
    admin = _ctx(tmp_path, agent="admin", authority="admin", admin=True)
    admin_atom, _ = boundary.put_atom(
        MemoryAtom(memory_id="m4", body="admin fact", share_group_id="group-a", agent_instance_id="agent-b"),
        context=admin,
        evidence=[{"source_ref": "legacy/group-a#m4", "digest": "d4"}],
        reason="manual admin correction",
    )
    assert admin_atom.agent_instance_id == "agent-b"
    deleted, _ = boundary.tombstone("m4", context=admin, reason="admin rollback")
    assert deleted.status == "deleted"


def test_tombstone_supersede_undo_is_compensating_and_decision_fields_present(tmp_path: Path):
    boundary = GovernanceV2(tmp_path)
    ctx = _ctx(tmp_path)
    old, _ = boundary.put_atom(MemoryAtom(memory_id="old", body="old"), context=ctx, evidence=[{"source_ref": "old", "digest": "o"}], reason="seed old")
    new, _ = boundary.put_atom(MemoryAtom(memory_id="new", body="new"), context=ctx, evidence=[{"source_ref": "new", "digest": "n"}], reason="seed new")
    decision = boundary.supersede(old.atom_id, new.atom_id, context=ctx, reason="newer fact", confidence=0.8)
    assert decision.undo_hash and decision.reason == "newer fact" and decision.confidence == 0.8
    compensated = boundary.undo(decision.decision_id, context=ctx, reason="review reverted")
    assert compensated.operation == "undo"
    tombstone, tombstone_decision = boundary.tombstone("old", context=ctx, reason="redaction requested")
    assert tombstone.status == "deleted"
    boundary.undo(tombstone_decision.decision_id, context=ctx, reason="redaction revoked")
    restored = boundary.memory.get_atom("old", scope=ctx.to_dict(), include_building=True)
    assert restored is not None and restored.status != "deleted"
    assert all(item.reason and item.undo_hash for item in boundary.list_decisions())


def test_evidence_context_link_unlink_and_body_rejection(tmp_path: Path):
    boundary = GovernanceV2(tmp_path)
    ctx = _ctx(tmp_path)
    atom, _ = boundary.put_atom(MemoryAtom(memory_id="subject", body="subject"), context=ctx, evidence=[{"source_ref": "subject", "digest": "s"}], reason="seed")
    evidence, _ = boundary.put_evidence(context=ctx, source_ref="v1/ref", digest="digest", authority="observed", reason="evidence ref")
    link, _ = boundary.link(evidence.evidence_id, "atom", atom.atom_id, context=ctx, reason="support")
    assert link.evidence_id == evidence.evidence_id
    removed, _ = boundary.unlink(evidence.evidence_id, "atom", atom.atom_id, context=ctx, reason="withdraw")
    assert removed == 1
    with pytest.raises(ValueError):
        boundary.put_evidence(context=ctx, source_ref="bad", digest="bad", metadata={"text": "raw"}, reason="reject")


def test_store_transactions_and_ledger_are_durable(tmp_path: Path):
    boundary = GovernanceV2(tmp_path)
    ctx = _ctx(tmp_path)
    atom, decision = boundary.put_atom(MemoryAtom(memory_id="tx", body="x"), context=ctx, evidence=[{"source_ref": "tx", "digest": "x"}], reason="transaction")
    with boundary.memory._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM atom_revisions WHERE atom_id=?", (atom.atom_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM domain_outbox WHERE aggregate_id=?", (atom.atom_id,)).fetchone()[0] >= 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    second = GovernanceV2(tmp_path)
    assert any(item.decision_id == decision.decision_id for item in second.list_decisions())


def test_supersede_revision_delta_outbox_is_atomic_and_retry_idempotent(tmp_path: Path):
    boundary = GovernanceV2(tmp_path)
    ctx = _ctx(tmp_path)
    old, _ = boundary.put_atom(MemoryAtom(memory_id="old", body="v1"), context=ctx, evidence=[{"source_ref": "old", "digest": "o"}], reason="seed old")
    new, _ = boundary.put_atom(MemoryAtom(memory_id="new", body="v2"), context=ctx, evidence=[{"source_ref": "new", "digest": "n"}], reason="seed new")
    decision = boundary.supersede(old.atom_id, new.atom_id, context=ctx, reason="replacement")
    with boundary.memory._connection() as conn:
        revisions = conn.execute("SELECT atom_id,revision FROM atom_revisions WHERE atom_id IN (?,?) ORDER BY atom_id,revision", (old.atom_id, new.atom_id)).fetchall()
        deltas = conn.execute("SELECT atom_id,from_revision,to_revision FROM atom_deltas WHERE atom_id IN (?,?)", (old.atom_id, new.atom_id)).fetchall()
        events = conn.execute("SELECT event_type,status FROM domain_outbox WHERE aggregate_id IN (?,?) ORDER BY sequence", (old.atom_id, new.atom_id)).fetchall()
    assert [int(row[1]) for row in revisions] == [1, 2, 1, 2]
    assert len(deltas) == 2 and all(int(row[1]) == 1 and int(row[2]) == 2 for row in deltas)
    assert sum(str(row[0]) == "atom.supersede" for row in events) == 2
    boundary.supersede(old.atom_id, new.atom_id, context=ctx, reason="replacement")
    assert boundary.memory.status()["revisions"] == 4
    assert len([item for item in boundary.list_decisions() if item.decision_id == decision.decision_id]) == 1
    assert boundary.memory.project_evidence(boundary.evidence)["pending"] == 0


def test_undo_hash_rejects_intervening_mutation(tmp_path: Path):
    boundary = GovernanceV2(tmp_path)
    ctx = _ctx(tmp_path)
    atom, _ = boundary.put_atom(MemoryAtom(memory_id="undo", body="v1"), context=ctx, evidence=[{"source_ref": "undo", "digest": "u"}], reason="seed")
    _, decision = boundary.tombstone("undo", context=ctx, reason="remove")
    current = boundary.memory.get_atom("undo", scope=ctx.to_dict(), include_building=True)
    assert current is not None
    current.metadata = {"changed": True}
    boundary.memory.put_atom(current, context=ctx)
    with pytest.raises(V2GovernanceError):
        boundary.undo(decision.decision_id, context=ctx, reason="stale undo")


def test_context_bool_aliases_and_direct_store_bypass_are_fail_closed(tmp_path: Path):
    with pytest.raises(V2ContextError):
        V2MutationContext(workspace_id=str(tmp_path), share_group_id="g", actor="a", admin="false")
    with pytest.raises(V2ContextError):
        V2MutationContext.from_value({"workspace": str(tmp_path), "workspace_id": str(tmp_path / "other"), "group_id": "g", "actor": "a"})
    with pytest.raises(V2ContextError):
        V2MutationContext.from_value({"workspace_id": str(tmp_path), "share_group_id": "g", "actor": "a", "admin": 2})
    with pytest.raises(V2ContextError):
        V2MutationContext.from_value({"workspace_id": str(tmp_path), "share_group_id": "g", "actor": "a", "automatic": "false"})
    with pytest.raises(V2ContextError):
        V2MutationContext.from_value({"workspace_id": str(tmp_path), "share_group_id": "g", "actor": "a", "automatic": object()})
    memory = MemoryAtomStore(tmp_path)
    with pytest.raises(PermissionError):
        memory.put_atom(MemoryAtom(memory_id="raw", body="b"))
    with pytest.raises(PermissionError):
        memory.delete("raw")
    with pytest.raises(PermissionError):
        memory.supersede("raw", "raw")
    with pytest.raises(PermissionError):
        EvidenceStore(tmp_path).put_evidence(source_ref="raw", digest="d")


def test_auto_requires_agent_and_unlink_replay_uses_one_receipt(tmp_path: Path):
    boundary = GovernanceV2(tmp_path)
    auto = _ctx(tmp_path, authority="auto")
    with pytest.raises(V2ScopeError):
        boundary.put_atom(MemoryAtom(memory_id="group-only", body="x"), context=auto, evidence=[{"source_ref": "x", "digest": "x"}], reason="reject group")
    atom, _ = boundary.put_atom(MemoryAtom(memory_id="subject", body="s", agent_instance_id="agent-a", project_ref="proj"), context=auto, evidence=[{"source_ref": "subject", "digest": "s"}], reason="seed")
    evidence, _ = boundary.put_evidence(context=auto, source_ref="unlink-ref", digest="u", reason="seed evidence")
    boundary.link(evidence.evidence_id, "atom", atom.atom_id, context=auto, reason="support")
    removed, first = boundary.unlink(evidence.evidence_id, "atom", atom.atom_id, context=auto, reason="withdraw", idempotency_key="unlink-1")
    replay_removed, replay = boundary.unlink(evidence.evidence_id, "atom", atom.atom_id, context=auto, reason="withdraw", idempotency_key="unlink-1")
    assert removed == 1 and replay_removed == 1 and replay.decision_id == first.decision_id
    assert len([item for item in boundary.list_decisions() if item.decision_id == first.decision_id]) == 1


def test_put_retry_is_same_put_receipt_and_key_conflict_is_atomic(tmp_path: Path):
    boundary = GovernanceV2(tmp_path)
    ctx = _ctx(tmp_path)
    first_atom, first = boundary.put_atom(
        MemoryAtom(memory_id="retry", body="v1"),
        context=ctx,
        evidence=[{"source_ref": "retry", "digest": "r1"}],
        reason="seed",
        idempotency_key="request-1",
    )
    before_status = boundary.memory.status()
    before_decisions = len(boundary.list_decisions())
    replay_atom, replay = boundary.put_atom(
        MemoryAtom(memory_id="retry", body="v1"),
        context=ctx,
        evidence=[{"source_ref": "retry", "digest": "r1"}],
        reason="seed",
        idempotency_key="request-1",
    )
    assert replay.decision_id == first.decision_id and replay.operation == "put"
    assert replay_atom.revision == first_atom.revision
    assert boundary.memory.status() == before_status
    assert len(boundary.list_decisions()) == before_decisions
    with pytest.raises(V2GovernanceError):
        boundary.put_atom(
            MemoryAtom(memory_id="retry", body="v2"),
            context=ctx,
            evidence=[{"source_ref": "retry", "digest": "r2"}],
            reason="changed payload",
            idempotency_key="request-1",
        )
    assert boundary.memory.get_atom("retry", scope=ctx.to_dict(), include_building=True).body == "v1"
    assert len(boundary.list_decisions()) == before_decisions


def test_ledger_failure_compensates_fact_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    boundary = GovernanceV2(tmp_path)
    ctx = _ctx(tmp_path)
    original = boundary._record

    def fail_record(*args, **kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(boundary, "_record", fail_record)
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        boundary.put_atom(
            MemoryAtom(memory_id="fault", body="v1"),
            context=ctx,
            evidence=[{"source_ref": "fault", "digest": "f"}],
            reason="fault",
            idempotency_key="fault-1",
        )
    monkeypatch.setattr(boundary, "_record", original)
    # The failed decision cannot expose an active fact; the compensating
    # tombstone keeps revision/outbox history while blocking visibility.
    fault = boundary.memory.get_atom("fault", scope=ctx.to_dict(), include_building=True)
    assert fault is None or fault.status == "deleted"


def test_concurrent_same_request_claims_once_and_replays(tmp_path: Path):
    """Two independent boundaries must not apply one request twice."""
    first_boundary = GovernanceV2(tmp_path)
    second_boundary = GovernanceV2(tmp_path)
    ctx = _ctx(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original_put = first_boundary.memory.put_atom

    def blocked_put(*args, **kwargs):
        entered.set()
        assert release.wait(5), "test mutation did not get released"
        return original_put(*args, **kwargs)

    first_boundary.memory.put_atom = blocked_put  # type: ignore[method-assign]
    atom = MemoryAtom(memory_id="concurrent", body="v1")
    kwargs = {
        "context": ctx,
        "evidence": [{"source_ref": "concurrent", "digest": "c"}],
        "reason": "concurrent put",
        "idempotency_key": "concurrent-1",
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(first_boundary.put_atom, atom, **kwargs)
        assert entered.wait(5)
        replay = pool.submit(second_boundary.put_atom, MemoryAtom(memory_id="concurrent", body="v1"), **kwargs)
        time.sleep(0.05)
        release.set()
        owner_atom, owner_decision = owner.result(timeout=10)
        replay_atom, replay_decision = replay.result(timeout=10)
    assert owner_atom.revision == replay_atom.revision == 1
    assert owner_decision.decision_id == replay_decision.decision_id
    assert first_boundary.memory.status()["revisions"] == 1
    assert len([item for item in first_boundary.list_decisions() if item.decision_id == owner_decision.decision_id]) == 1


def test_concurrent_same_key_conflict_rejected_before_mutation(tmp_path: Path):
    """A different fingerprint cannot mutate while a request claim is active."""
    first_boundary = GovernanceV2(tmp_path)
    second_boundary = GovernanceV2(tmp_path)
    ctx = _ctx(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original_put = first_boundary.memory.put_atom

    def blocked_put(*args, **kwargs):
        entered.set()
        assert release.wait(5), "test mutation did not get released"
        return original_put(*args, **kwargs)

    first_boundary.memory.put_atom = blocked_put  # type: ignore[method-assign]
    common = {
        "context": ctx,
        "evidence": [{"source_ref": "conflict", "digest": "c"}],
        "reason": "conflict put",
        "idempotency_key": "conflict-1",
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(first_boundary.put_atom, MemoryAtom(memory_id="conflict", body="v1"), **common)
        assert entered.wait(5)
        conflict = pool.submit(second_boundary.put_atom, MemoryAtom(memory_id="conflict", body="v2"), **common)
        with pytest.raises(V2GovernanceError):
            conflict.result(timeout=5)
        release.set()
        owner_atom, owner_decision = owner.result(timeout=10)
    assert owner_atom.body == "v1"
    assert owner_decision.operation == "put"
    assert first_boundary.memory.get_atom("conflict", scope=ctx.to_dict(), include_building=True).body == "v1"
    assert len(first_boundary.list_decisions()) == 1


def test_orphaned_claim_fails_closed_after_writer_loss(tmp_path: Path):
    boundary = GovernanceV2(tmp_path)
    ctx = _ctx(tmp_path)
    _, claim = boundary._claim_request(ctx, "put", "crashed-1", "fingerprint-1")
    assert claim is not None
    with pytest.raises(V2GovernanceError, match="in flight"):
        boundary._claim_request(ctx, "put", "crashed-1", "fingerprint-1")
    with sqlite3.connect(boundary.ledger_path) as conn:
        row = conn.execute(
            "SELECT state,claim_token FROM request_ledger WHERE actor=? AND idempotency_key=?",
            (ctx.actor, "crashed-1"),
        ).fetchone()
    assert row[0] == "claimed" and row[1] == claim.token
