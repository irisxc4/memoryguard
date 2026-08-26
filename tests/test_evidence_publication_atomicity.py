"""Evidence publication must not leak un-evidenced atoms."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import V2MutationContext
from memoryguard.memory import MemoryAtom, MemoryAtomStore, MemoryReadScope


def _ctx(root: Path, group: str = "pub-group", agent: str = "agent-a") -> V2MutationContext:
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


def _scope(root: Path, group: str = "pub-group") -> MemoryReadScope:
    return MemoryReadScope(
        workspace_id=str(root.resolve()),
        share_group_id=group,
        admin=True,
    )


def _atom(root: Path, memory_id: str, body: str, *, visibility: str = "ready") -> MemoryAtom:
    return MemoryAtom(
        memory_id=memory_id,
        body=body,
        kind="fact",
        status="active",
        visibility=visibility,
        workspace_id=str(root.resolve()),
        share_group_id="pub-group",
        agent_instance_id="agent-a",
    )


def _counts(memory: MemoryAtomStore) -> tuple[int, int, int]:
    with sqlite3.connect(memory.db_path) as conn:
        atoms = int(conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0])
        outbox = int(conn.execute("SELECT COUNT(*) FROM domain_outbox").fetchone()[0])
        failed = int(conn.execute("SELECT COUNT(*) FROM domain_outbox WHERE status='failed'").fetchone()[0])
    return atoms, outbox, failed


def test_invalid_authority_mutates_nothing(tmp_path: Path):
    memory = MemoryAtomStore(tmp_path)
    with pytest.raises(ValueError, match="unknown evidence authority"):
        memory.put_atom(
            _atom(tmp_path, "memory-a", "leaked body", visibility="ready"),
            evidence=[{"source_ref": "fixture:memory-a", "authority": "test"}],
            context=_ctx(tmp_path),
        )
    atoms, outbox, failed = _counts(memory)
    assert atoms == 0
    assert outbox == 0
    assert failed == 0
    scope = _scope(tmp_path)
    assert memory.get_atom("memory-a", scope=scope) is None
    assert memory.get_atom("memory-a", scope=scope, include_building=True) is None
    assert memory.list_atoms(scope=scope, include_building=True) == []
    assert memory.pending_outbox(include_failed=True) == []
    assert "test" not in EvidenceStore.ALLOWED_AUTHORITIES


def test_unknown_authority_rejection_remains_fail_closed(tmp_path: Path):
    evidence = EvidenceStore(tmp_path)
    with pytest.raises(ValueError, match="unknown evidence authority"):
        evidence.put_evidence(
            source_ref="attack",
            authority="caller-forged",
            context=_ctx(tmp_path),
        )
    with sqlite3.connect(evidence.db_path) as conn:
        assert int(conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]) == 0


def test_transient_projection_failure_keeps_new_atom_invisible_until_retry(tmp_path: Path, monkeypatch):
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    scope = _scope(tmp_path)
    persisted = memory.put_atom(
        _atom(tmp_path, "memory-a", "new atom body", visibility="ready"),
        evidence=[{"source_ref": "fixture:memory-a", "authority": "observed"}],
        context=_ctx(tmp_path),
    )
    assert memory.get_atom("memory-a", scope=scope) is None
    assert memory.list_atoms(scope=scope) == []
    assert memory.search("new atom", scope=scope) == []
    hidden = memory.get_atom("memory-a", scope=scope, include_building=True)
    assert hidden is not None
    assert hidden.visibility == "building"
    assert hidden.status == "active"
    assert _revision_numbers(memory, tmp_path, "memory-a") == []
    assert memory.replay_revision(persisted.atom_id) is None
    assert memory.replay_revision(persisted.atom_id, 1) is None

    def fail(*_args, **_kwargs):
        raise RuntimeError("evidence fault")

    monkeypatch.setattr(evidence, "project_batch", fail)
    failed = memory.project_evidence(evidence)
    assert failed["failed"] > 0
    assert memory.pending_outbox(include_failed=True)
    assert memory.get_atom("memory-a", scope=scope) is None
    assert memory.list_atoms(scope=scope) == []
    assert memory.search("new atom", scope=scope) == []
    assert memory.evidence_ids_for_atom(persisted.atom_id) == []
    assert _revision_numbers(memory, tmp_path, "memory-a") == []
    assert memory.replay_revision(persisted.atom_id) is None
    assert memory.replay_revision(persisted.atom_id, 1) is None
    with sqlite3.connect(memory.db_path) as conn:
        receipts = int(conn.execute("SELECT COUNT(*) FROM evidence_projection_receipts").fetchone()[0])
    assert receipts == 0

    monkeypatch.undo()
    retry = memory.project_evidence(EvidenceStore(tmp_path))
    assert retry["failed"] == 0
    assert memory.pending_outbox(include_failed=True) == []
    visible = memory.get_atom("memory-a", scope=scope)
    assert visible is not None
    assert visible.body == "new atom body"
    assert visible.visibility in {"ready", "active"}
    assert visible.revision == 1
    assert _revision_numbers(memory, tmp_path, "memory-a") == [1]
    replayed = memory.replay_revision(visible.atom_id, 1)
    assert replayed is not None
    assert replayed.body == "new atom body"
    ids = memory.evidence_ids_for_atom(visible.atom_id)
    assert len(ids) == 1
    assert memory.search("new atom", scope=scope)
    retry_again = memory.project_evidence(EvidenceStore(tmp_path))
    assert retry_again["projected"] == 0
    assert memory.evidence_ids_for_atom(visible.atom_id) == ids


def test_failed_update_preserves_previous_visible_revision(tmp_path: Path, monkeypatch):
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    scope = _scope(tmp_path)
    first = memory.put_atom(
        _atom(tmp_path, "memory-a", "committed body", visibility="ready"),
        evidence=[{"source_ref": "fixture:memory-a", "authority": "observed"}],
        context=_ctx(tmp_path),
    )
    assert memory.project_evidence(evidence)["failed"] == 0
    committed = memory.get_atom("memory-a", scope=scope)
    assert committed is not None
    assert committed.body == "committed body"
    assert committed.revision == 1
    assert committed.visibility in {"ready", "active"}
    previous_revision = committed.revision
    previous_visibility = committed.visibility

    def fail(*_args, **_kwargs):
        raise RuntimeError("evidence fault")

    evidence_fail = EvidenceStore(tmp_path)
    monkeypatch.setattr(evidence_fail, "project_batch", fail)
    memory.put_atom(
        _atom(tmp_path, "memory-a", "un-evidenced update", visibility="ready"),
        evidence=[{"source_ref": "fixture:memory-a-update", "authority": "observed"}],
        context=_ctx(tmp_path),
    )
    failed = memory.project_evidence(evidence_fail)
    assert failed["failed"] > 0
    still = memory.get_atom("memory-a", scope=scope)
    assert still is not None
    assert still.body == "committed body"
    assert still.revision == previous_revision
    assert still.visibility == previous_visibility
    assert memory.search("committed body", scope=scope)
    assert memory.search("un-evidenced update", scope=scope) == []
    assert memory.evidence_ids_for_atom(first.atom_id) == memory.evidence_ids_for_atom(still.atom_id)

    monkeypatch.undo()
    retry = memory.project_evidence(EvidenceStore(tmp_path))
    assert retry["failed"] == 0
    updated = memory.get_atom("memory-a", scope=scope)
    assert updated is not None
    assert updated.body == "un-evidenced update"
    assert updated.revision == previous_revision + 1
    assert updated.visibility in {"ready", "active"}
    assert len(memory.evidence_ids_for_atom(updated.atom_id)) == 2
    assert memory.search("un-evidenced update", scope=scope)
    assert memory.pending_outbox(include_failed=True) == []


def _revision_numbers(memory: MemoryAtomStore, root: Path, memory_id: str) -> list[int]:
    return [
        int(item["revision"])
        for item in memory.list_revisions(scope=_scope(root), memory_id=memory_id)
    ]


def _acl_metadata(memory: MemoryAtomStore, atom_id: str) -> list[dict]:
    with sqlite3.connect(memory.db_path) as conn:
        rows = conn.execute(
            "SELECT metadata_json,effect FROM scope_acl WHERE atom_id=? ORDER BY created_at,acl_id",
            (atom_id,),
        ).fetchall()
    result = []
    for row in rows:
        payload = json.loads(row[0] or "{}")
        payload["effect"] = row[1]
        result.append(payload)
    return result


def test_staged_revision_hidden_until_projection(tmp_path: Path, monkeypatch):
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    scope = _scope(tmp_path)
    first = memory.put_atom(
        _atom(tmp_path, "memory-a", "committed body", visibility="ready"),
        evidence=[{"source_ref": "fixture:memory-a", "authority": "observed"}],
        context=_ctx(tmp_path),
    )
    assert memory.project_evidence(evidence)["failed"] == 0
    committed = memory.get_atom("memory-a", scope=scope)
    assert committed is not None
    assert _revision_numbers(memory, tmp_path, "memory-a") == [1]
    original = memory.replay_revision(first.atom_id, 1)
    assert original is not None
    assert original.body == "committed body"

    def fail(*_args, **_kwargs):
        raise RuntimeError("evidence fault")

    evidence_fail = EvidenceStore(tmp_path)
    monkeypatch.setattr(evidence_fail, "project_batch", fail)
    memory.put_atom(
        _atom(tmp_path, "memory-a", "staged body", visibility="ready"),
        evidence=[{"source_ref": "fixture:memory-a-staged", "authority": "observed"}],
        context=_ctx(tmp_path),
    )
    assert memory.project_evidence(evidence_fail)["failed"] > 0
    assert _revision_numbers(memory, tmp_path, "memory-a") == [1]
    assert memory.replay_revision(first.atom_id, 2) is None
    assert memory.replay_revision(first.atom_id) is not None
    assert memory.replay_revision(first.atom_id).body == "committed body"
    assert memory.replay_revision(first.atom_id, 1).body == "committed body"
    still = memory.get_atom("memory-a", scope=scope)
    assert still is not None
    assert still.revision == 1
    assert still.body == "committed body"

    monkeypatch.undo()
    assert memory.project_evidence(EvidenceStore(tmp_path))["failed"] == 0
    assert _revision_numbers(memory, tmp_path, "memory-a") == [1, 2]
    promoted = memory.replay_revision(first.atom_id, 2)
    assert promoted is not None
    assert promoted.body == "staged body"
    assert memory.get_atom("memory-a", scope=scope).body == "staged body"
    prior = memory.replay_revision(first.atom_id, 1)
    assert prior is not None
    assert prior.body == "committed body"


def test_staged_acl_and_source_mappings_apply_once_after_projection(tmp_path: Path, monkeypatch):
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    first = memory.put_atom(
        _atom(tmp_path, "memory-a", "committed body", visibility="ready"),
        evidence=[{"source_ref": "fixture:memory-a", "authority": "observed"}],
        source_mappings=[{
            "source_domain": "fixture",
            "source_ref": "committed-source",
            "source_record_id": "rec-1",
            "digest": "d1",
        }],
        acl={"audience_tag": "committed-acl"},
        context=_ctx(tmp_path),
    )
    assert memory.project_evidence(evidence)["failed"] == 0
    before_maps = memory.list_source_mappings(atom_id=first.atom_id)
    assert [item["source_ref"] for item in before_maps] == ["committed-source"]
    before_acl = _acl_metadata(memory, first.atom_id)
    assert any(item.get("audience_tag") == "committed-acl" for item in before_acl)

    def fail(*_args, **_kwargs):
        raise RuntimeError("evidence fault")

    evidence_fail = EvidenceStore(tmp_path)
    monkeypatch.setattr(evidence_fail, "project_batch", fail)
    memory.put_atom(
        _atom(tmp_path, "memory-a", "staged body", visibility="ready"),
        evidence=[{"source_ref": "fixture:memory-a-staged", "authority": "observed"}],
        source_mappings=[{
            "source_domain": "fixture",
            "source_ref": "staged-source",
            "source_record_id": "rec-2",
            "digest": "d2",
        }],
        acl={"audience_tag": "staged-acl"},
        context=_ctx(tmp_path),
    )
    assert memory.project_evidence(evidence_fail)["failed"] > 0
    pending_maps = memory.list_source_mappings(atom_id=first.atom_id)
    assert [item["source_ref"] for item in pending_maps] == ["committed-source"]
    pending_acl = _acl_metadata(memory, first.atom_id)
    assert any(item.get("audience_tag") == "committed-acl" for item in pending_acl)
    assert all(item.get("audience_tag") != "staged-acl" for item in pending_acl)

    monkeypatch.undo()
    assert memory.project_evidence(EvidenceStore(tmp_path))["failed"] == 0
    after_maps = memory.list_source_mappings(atom_id=first.atom_id)
    assert [item["source_ref"] for item in after_maps] == ["committed-source", "staged-source"]
    after_acl = _acl_metadata(memory, first.atom_id)
    assert any(item.get("audience_tag") == "staged-acl" for item in after_acl)
    retry = memory.project_evidence(EvidenceStore(tmp_path))
    assert retry["projected"] == 0
    assert [item["source_ref"] for item in memory.list_source_mappings(atom_id=first.atom_id)] == [
        "committed-source",
        "staged-source",
    ]
    assert sum(1 for item in _acl_metadata(memory, first.atom_id) if item.get("audience_tag") == "staged-acl") == 1


def test_same_evidence_edit_then_lock_publishes_locked_revision(tmp_path: Path):
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    scope = _scope(tmp_path)
    shared_evidence = [{"source_ref": "fixture:memory-a", "authority": "observed"}]
    created = memory.put_atom(
        _atom(tmp_path, "memory-a", "original body", visibility="ready"),
        evidence=shared_evidence,
        context=_ctx(tmp_path),
    )
    assert memory.project_evidence(evidence)["failed"] == 0
    visible = memory.get_atom("memory-a", scope=scope)
    assert visible is not None
    assert visible.locked is False

    memory.put_atom(
        _atom(tmp_path, "memory-a", "edited body", visibility="ready"),
        evidence=shared_evidence,
        context=_ctx(tmp_path),
    )
    assert memory.project_evidence(EvidenceStore(tmp_path))["failed"] == 0
    edited = memory.get_atom("memory-a", scope=scope)
    assert edited is not None
    assert edited.body == "edited body"
    assert edited.locked is False
    edited_revision = edited.revision

    locked_item = _atom(tmp_path, "memory-a", "edited body", visibility="ready")
    locked_item.locked = True
    memory.put_atom(
        locked_item,
        evidence=shared_evidence,
        context=_ctx(tmp_path),
    )
    projected = memory.project_evidence(EvidenceStore(tmp_path))
    assert projected["failed"] == 0
    assert projected["projected"] > 0
    current = memory.get_atom("memory-a", scope=scope)
    assert current is not None
    assert current.body == "edited body"
    assert current.locked is True
    assert current.revision == edited_revision + 1
    assert current.visibility in {"ready", "active"}
    assert len(memory.evidence_ids_for_atom(created.atom_id)) == 3

    replay_item = _atom(tmp_path, "memory-a", "edited body", visibility="ready")
    replay_item.locked = True
    replayed = memory.put_atom(
        replay_item,
        evidence=shared_evidence,
        context=_ctx(tmp_path),
    )
    assert replayed.revision == current.revision
    assert replayed.locked is True
    assert memory.pending_outbox(include_failed=True) == []
    assert memory.get_atom("memory-a", scope=scope).locked is True
    assert memory.project_evidence(EvidenceStore(tmp_path))["projected"] == 0
