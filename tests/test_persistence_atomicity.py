"""V2 persistence, governance, projection, and lifecycle atomicity tests."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import (
    GovernanceV2,
    V2GovernanceError,
    V2MutationContext,
    V2ScopeError,
)
from memoryguard.memory import MemoryAtom, MemoryAtomStore, MemoryReadScope
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState


def _activate_v2(root: Path) -> tuple[MemoryAtomStore, EvidenceStore, GovernanceV2, GroupControlService]:
    initialize_all(WorkspaceV2Layout(root))
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    manager = ManifestManager(root)
    manager.transition(ManifestState.V2_BUILDING, migration_id="persistence-atomicity")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="persistence-source",
        target_digest="persistence-target",
        manifest_digest="persistence-manifest",
        digests={"validator_passed": True, "checkpoints": {"core": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE
    return memory, evidence, governance, GroupControlService(root, write=True)


def _context(root: Path, *, agent: str = "agent-a", group: str = "atomic", admin: bool = True, authority: str = "manual") -> V2MutationContext:
    workspace = str(root.resolve())
    return V2MutationContext(
        workspace_id=workspace,
        share_group_id=group,
        agent_instance_id=agent,
        project_ref=workspace,
        provider="codex",
        runtime_role="test",
        actor=agent,
        authority=authority,
        admin=admin,
    )


def _atom(root: Path, memory_id: str, *, body: str | None = None, group: str = "atomic", agent: str = "agent-a", metadata: dict | None = None, status: str = "active") -> MemoryAtom:
    workspace = str(root.resolve())
    return MemoryAtom(
        memory_id=memory_id,
        body=body or f"body-{memory_id}",
        kind="procedure",
        status=status,
        confidence=0.8,
        injection_policy="always",
        priority=10,
        agent_instance_id=agent,
        share_group_id=group,
        project_ref=workspace,
        provider="codex",
        runtime_role="test",
        workspace_id=workspace,
        metadata=metadata or {},
    )


def _seed(root: Path, boundary: GovernanceV2, memory_id: str, *, body: str | None = None, group: str = "atomic", agent: str = "agent-a", metadata: dict | None = None) -> MemoryAtom:
    atom, _decision = boundary.put_atom(
        _atom(root, memory_id, body=body, group=group, agent=agent, metadata=metadata),
        context=_context(root, agent=agent, group=group),
        evidence=[{"source_ref": f"security/{memory_id}", "digest": memory_id}],
        reason=f"seed {memory_id}",
        idempotency_key=f"seed-{memory_id}",
    )
    return atom


def _scope(root: Path, group: str = "atomic") -> MemoryReadScope:
    return MemoryReadScope(
        workspace_id=str(root.resolve()),
        share_group_id=group,
        admin=True,
    )


def _native_context(root: Path, group: str = "atomic", agent: str = "agent-a"):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id=f"persistence-{agent}",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(root.resolve()),
        share_group_id=group,
        project_ref=str(root.resolve()),
        provider="codex",
        runtime_role="test",
        entrypoint="persistence-test",
    )


def _native_port(root: Path) -> NativeV2RuntimePort:
    return NativeV2RuntimePort(
        root,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 1},
    )


def test_rule_create_decision_failure_rolls_back_record_and_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    memory, _evidence, boundary, _groups = _activate_v2(tmp_path)
    context = _context(tmp_path)

    def fail(*_args, **_kwargs):
        raise RuntimeError("decision fault")

    monkeypatch.setattr(boundary, "_record", fail)
    with pytest.raises(RuntimeError, match="decision fault"):
        boundary.put_atom(
            _atom(tmp_path, "r"),
            context=context,
            evidence=[{"source_ref": "atomic/r", "digest": "r"}],
            reason="atomic create",
            idempotency_key="atomic-create-r",
        )

    persisted = memory.get_atom("r", scope=_scope(tmp_path), include_building=True)
    assert persisted is None or persisted.status == "deleted"
    assert boundary.list_decisions() == []


def test_rule_create_undo_decision_failure_rolls_back_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    memory, _evidence, boundary, _groups = _activate_v2(tmp_path)
    context = _context(tmp_path)
    _seed(tmp_path, boundary, "r")

    original = boundary._record

    def fail(*_args, **_kwargs):
        raise RuntimeError("inverse fault")

    monkeypatch.setattr(boundary, "_record", fail)
    with pytest.raises(RuntimeError, match="inverse fault"):
        boundary.tombstone("r", context=context, reason="rollback delete", idempotency_key="delete-r")
    monkeypatch.setattr(boundary, "_record", original)

    restored = memory.get_atom("r", scope=_scope(tmp_path), include_building=True)
    assert restored is not None and restored.status == "active"


def test_cross_domain_failure_reports_committed_degraded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    memory, evidence, boundary, _groups = _activate_v2(tmp_path)
    context = _context(tmp_path)
    atom, _decision = boundary.put_atom(
        _atom(tmp_path, "r"),
        context=context,
        evidence=[{"source_ref": "atomic/r", "digest": "r"}],
        reason="projection failure fixture",
        idempotency_key="projection-failure-r",
    )

    def fail_projection(_events):
        raise OSError("evidence sink unavailable")

    monkeypatch.setattr(evidence, "project_batch", fail_projection)
    result = memory.project_evidence(evidence)
    assert result["failed"] >= 1
    assert result["pending"] >= 1
    assert memory.get_atom("r", scope=_scope(tmp_path), include_building=True) is not None


def test_deduplicated_decision_targets_existing_record(tmp_path: Path):
    _memory, _evidence, boundary, _groups = _activate_v2(tmp_path)
    context = _context(tmp_path)
    original = _seed(tmp_path, boundary, "original", body="body-original")

    decision = boundary.record_deduplication(
        original,
        context=context,
        request_payload={"candidate_memory_id": "candidate", "body_digest": "body-original"},
        reason="same body deduplication",
        idempotency_key="dedup-candidate",
    )
    replay = boundary.record_deduplication(
        original,
        context=context,
        request_payload={"candidate_memory_id": "candidate", "body_digest": "body-original"},
        reason="same body deduplication",
        idempotency_key="dedup-candidate",
    )
    assert decision.operation == "deduplicate"
    assert decision.target == {"atom_id": original.atom_id, "memory_id": "original"}
    assert replay.decision_id == decision.decision_id
    assert len([item for item in boundary.list_decisions() if item.decision_id == decision.decision_id]) == 1


def test_owner_scope_rejects_cross_agent_update_and_admin_can_update(tmp_path: Path):
    memory, _evidence, boundary, _groups = _activate_v2(tmp_path)
    _seed(tmp_path, boundary, "owned", metadata={"owner_agent_id": "agent-a"})

    with pytest.raises(V2ScopeError, match="mutation logical record is outside context") as rejected:
        boundary.put_atom(
            _atom(tmp_path, "owned", body="forged", agent="agent-b"),
            context=_context(tmp_path, agent="agent-b", admin=False),
            evidence=[{"source_ref": "atomic/forged", "digest": "forged"}],
            reason="cross agent update",
            idempotency_key="forged-owned-update",
        )
    assert "UNIQUE" not in str(rejected.value)

    existing = memory.get_atom("owned", scope=_scope(tmp_path), include_building=True)
    assert existing is not None
    updated, _decision = boundary.put_atom(
        MemoryAtom.from_value(
            existing,
            atom_id=existing.atom_id,
            body="admin correction",
            agent_instance_id="agent-b",
            metadata={"owner_agent_id": "agent-a"},
        ),
        context=_context(tmp_path, agent="admin", admin=True, authority="admin"),
        evidence=[{"source_ref": "atomic/admin", "digest": "admin"}],
        reason="admin correction",
        idempotency_key="admin-owned-update",
    )
    assert updated.body == "admin correction"
    assert updated.atom_id == existing.atom_id
    assert memory.get_atom("owned", scope=_scope(tmp_path), include_building=True).metadata["owner_agent_id"] == "agent-a"
    assert len(memory.list_atoms(scope=_scope(tmp_path), include_building=True)) == 1


def test_migration_failure_leaves_no_partial_v2_schema(tmp_path: Path):
    memory, _evidence, _boundary, _groups = _activate_v2(tmp_path)
    del memory
    db_path = WorkspaceV2Layout(tmp_path).memory_db
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE memory_schema_meta SET version=99, marker='future-memory-schema' WHERE domain='memory'"
        )
        conn.commit()
    corrupted = db_path.read_bytes()

    with pytest.raises(RuntimeError, match="unsupported memory phase2 schema metadata"):
        MemoryAtomStore(tmp_path)
    assert corrupted == db_path.read_bytes()


def test_last_sibling_unlink_removes_final_evidence_relation(tmp_path: Path):
    _memory, evidence, boundary, _groups = _activate_v2(tmp_path)
    context = _context(tmp_path)
    atom = _seed(tmp_path, boundary, "parent")
    first, _ = boundary.put_evidence(
        context=context, source_ref="feedback/1", digest="feedback-1", reason="first sibling"
    )
    second, _ = boundary.put_evidence(
        context=context, source_ref="feedback/2", digest="feedback-2", reason="second sibling"
    )
    boundary.link(first.evidence_id, "atom", atom.atom_id, context=context, reason="first link")
    boundary.link(second.evidence_id, "atom", atom.atom_id, context=context, reason="second link")

    removed_first, _ = boundary.unlink(first.evidence_id, "atom", atom.atom_id, context=context, reason="revoke first")
    assert removed_first == 1
    with sqlite3.connect(evidence.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence_links WHERE subject_id=?", (atom.atom_id,)).fetchone()[0] == 1

    removed_second, _ = boundary.unlink(second.evidence_id, "atom", atom.atom_id, context=context, reason="revoke second")
    assert removed_second == 1
    with sqlite3.connect(evidence.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence_links WHERE subject_id=?", (atom.atom_id,)).fetchone()[0] == 0


def test_lifecycle_supersede_round_trip_is_atomic(tmp_path: Path):
    memory, _evidence, boundary, _groups = _activate_v2(tmp_path)
    context = _context(tmp_path)
    old = _seed(tmp_path, boundary, "old")
    new = _seed(tmp_path, boundary, "new")

    decision = boundary.supersede(old.atom_id, new.atom_id, context=context, reason="newer fact")
    assert decision.target == {"old": old.atom_id, "new": new.atom_id}
    assert memory.get_atom("old", scope=_scope(tmp_path), include_building=True).status == "superseded"
    assert old.memory_id in memory.get_atom("new", scope=_scope(tmp_path), include_building=True).supersedes

    undo = boundary.undo(decision.decision_id, context=context, reason="review reverted")
    assert undo.operation == "undo"
    assert memory.get_atom("old", scope=_scope(tmp_path), include_building=True).status == "active"
    assert memory.get_atom("new", scope=_scope(tmp_path), include_building=True).supersedes == []


def test_lifecycle_conflict_resolution_preserves_unrelated_members(tmp_path: Path):
    memory, _evidence, boundary, _groups = _activate_v2(tmp_path)
    group = "atomic"
    _seed(
        tmp_path, boundary, "conflict-keep", metadata={
            "conflict_group_id": "group-1",
            "conflict_status": "unresolved",
            "conflict_reason": "existing conflict",
        },
    )
    _seed(
        tmp_path, boundary, "conflict-drop", metadata={
            "conflict_group_id": "group-1",
            "conflict_status": "unresolved",
            "conflict_reason": "existing conflict",
        },
    )
    _seed(
        tmp_path, boundary, "unrelated", metadata={
            "conflict_group_id": "group-2",
            "conflict_status": "unresolved",
            "conflict_reason": "unrelated conflict",
        },
    )
    _seed(
        tmp_path, boundary, "unrelated-peer", metadata={
            "conflict_group_id": "group-2",
            "conflict_status": "unresolved",
            "conflict_reason": "unrelated conflict",
        },
    )

    port = _native_port(tmp_path)
    context = _native_context(tmp_path, group)
    conflicts = port.dispatch_gui("get_conflicts", [group], context=context, generation=1, state="V2_ACTIVE")
    assert conflicts["ok"] is True
    assert {item["group_id"] for item in conflicts["data"]["conflicts"]} == {"group-1", "group-2"}
    resolved = port.dispatch_gui(
        "resolve_conflict", ["group-1", "conflict-keep", group],
        context=context, generation=1, mutation=True, state="V2_ACTIVE",
    )
    assert resolved["ok"] is True, resolved
    assert resolved["data"]["deleted_memory_ids"] == ["conflict-drop"]
    assert memory.get_atom("conflict-keep", scope=_scope(tmp_path), include_building=True).status == "active"
    assert memory.get_atom("conflict-drop", scope=_scope(tmp_path), include_building=True).status == "deleted"
    assert memory.get_atom("unrelated", scope=_scope(tmp_path), include_building=True).status == "active"
    assert memory.get_atom("unrelated-peer", scope=_scope(tmp_path), include_building=True).status == "active"


def test_lifecycle_quarantine_round_trip_writes_release_tombstone(tmp_path: Path):
    memory, _evidence, boundary, _groups = _activate_v2(tmp_path)
    _seed(tmp_path, boundary, "quarantine-me")
    port = _native_port(tmp_path)
    context = _native_context(tmp_path)

    quarantined = port.dispatch_gui(
        "neuron_decide", ["quarantine-me", "quarantine", "manual review", True, None, "", ""],
        context=context, generation=1, mutation=True, state="V2_ACTIVE",
    )
    assert quarantined["ok"] is True, quarantined
    assert quarantined["data"]["memory_status"] == "quarantined"
    queue = port.dispatch_gui("get_quarantine", ["atomic"], context=context, generation=1, state="V2_ACTIVE")
    assert queue["ok"] is True and queue["data"]["total"] == 1
    entry = queue["data"]["quarantine"][0]
    released = port.dispatch_gui(
        "release_quarantine", [entry["quarantine_id"], "atomic"],
        context=context, generation=1, mutation=True, state="V2_ACTIVE",
    )
    assert released["ok"] is True, released
    assert memory.get_atom("quarantine-me", scope=_scope(tmp_path), include_building=True).status == "active"


def test_lifecycle_decision_failure_rolls_back_every_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    memory, _evidence, boundary, _groups = _activate_v2(tmp_path)
    context = _context(tmp_path)
    old = _seed(tmp_path, boundary, "old")
    new = _seed(tmp_path, boundary, "new")

    def fail(*_args, **_kwargs):
        raise RuntimeError("decision fault")

    monkeypatch.setattr(boundary, "_record", fail)
    with pytest.raises(RuntimeError, match="decision fault"):
        boundary.supersede(old.atom_id, new.atom_id, context=context, reason="fault injection")
    assert memory.get_atom("old", scope=_scope(tmp_path), include_building=True).status == "active"
    assert memory.get_atom("new", scope=_scope(tmp_path), include_building=True).supersedes == []


def test_lifecycle_automatic_scope_guard_and_manual_broad_override(tmp_path: Path):
    _memory, _evidence, boundary, _groups = _activate_v2(tmp_path)
    automatic = V2MutationContext(
        workspace_id=str(tmp_path.resolve()),
        share_group_id="atomic",
        agent_instance_id="",
        project_ref="",
        actor="automatic-organizer",
        authority="auto",
        admin=False,
    )
    with pytest.raises(V2ScopeError):
        boundary.put_atom(
            _atom(tmp_path, "auto-broad"),
            context=automatic,
            evidence=[{"source_ref": "atomic/auto", "digest": "auto"}],
            reason="automatic broad scope",
        )

    manual = _context(tmp_path, agent="admin", admin=True, authority="admin")
    result, _decision = boundary.put_atom(
        _atom(tmp_path, "manual-broad", agent="agent-a"),
        context=manual,
        evidence=[{"source_ref": "atomic/manual", "digest": "manual"}],
        reason="manual broad scope",
        idempotency_key="manual-broad",
    )
    assert result.share_group_id == "atomic"
    assert result.agent_instance_id == "agent-a"

    evidence, _ = boundary.put_evidence(
        context=automatic,
        source_ref="atomic/automatic-system",
        digest="automatic-system",
        reason="automatic evidence scope",
    )
    with pytest.raises(V2ScopeError):
        boundary.link(evidence.evidence_id, "system", "atomic", context=automatic, reason="automatic system link")
