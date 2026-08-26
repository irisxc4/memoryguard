from __future__ import annotations

import hashlib
import base64
import json
import os
from pathlib import Path
import sqlite3

import pytest

from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import V2MutationContext
from memoryguard.memory import MemoryAtom, MemoryAtomStore
from memoryguard.migration.memory import V1MemoryMigrator


def _ctx(root: Path, group: str = "g", *, agent: str = "", project: str = "") -> V2MutationContext:
    return V2MutationContext(
        workspace_id=str(root),
        share_group_id=group,
        agent_instance_id=agent,
        project_ref=project,
        actor="v2-test",
        authority="manual",
    )


def _v1_group(root: Path, group: str, rows: list[tuple]) -> Path:
    path = root / ".memoryguard" / "shared-memory" / group / "memory.db"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE records (memory_id TEXT PRIMARY KEY, body TEXT, kind TEXT, status TEXT, confidence REAL, locked INTEGER, injection_policy TEXT, priority INTEGER, supersedes TEXT, provenance TEXT, agent_instance_id TEXT, created_at TEXT, updated_at TEXT, canonical_hash TEXT, dedup_domain TEXT)"
    )
    conn.execute("CREATE TABLE decisions (event_id TEXT PRIMARY KEY, actor TEXT, action TEXT, target_ids TEXT, created_at TEXT)")
    conn.executemany("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.execute("INSERT INTO decisions VALUES (?,?,?,?,?)", (f"decision-{group}", "user", "accept", "[]", "now"))
    conn.commit()
    conn.close()
    return path


def _row(memory_id: str, body: str = "body", provenance: str = "[]", supersedes: str = "[]") -> tuple:
    return (memory_id, body, "fact", "active", 0.8, 1, "always", 2, supersedes, provenance, "agent", "now", "now", hashlib.sha256(body.encode()).hexdigest(), "relevant")


def test_multigroup_same_memory_id_isolated_and_each_atom_has_evidence(tmp_path: Path):
    _v1_group(tmp_path, "g1", [_row("same", "one")])
    _v1_group(tmp_path, "g2", [_row("same", "two")])
    migrator = V1MemoryMigrator(tmp_path)

    first = migrator.migrate()
    second = migrator.migrate()

    assert first.ok and second.ok
    atoms = [item for group in ("g1", "g2") for item in migrator.memory_store.list_atoms(scope={"share_group_id": group, "workspace_id": str(tmp_path)}, include_building=True)]
    assert len(atoms) == 2
    assert {atom.share_group_id for atom in atoms} == {"g1", "g2"}
    assert len({atom.atom_id for atom in atoms}) == 2
    assert migrator.memory_store.validate(migrator.evidence_store).orphan_count == 0
    assert migrator.memory_store.status()["revisions"] == 2
    with migrator.memory_store._connection() as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_skips_fts_shadow_rows_and_keeps_outbox_authoritative(tmp_path: Path):
    rows = [_row(f"fts-{index}", f"body-{index}") for index in range(12)]
    path = _v1_group(tmp_path, "fts", rows)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE VIRTUAL TABLE records_fts USING fts5(memory_id UNINDEXED, body, content='records', content_rowid='rowid')")
        conn.executescript("CREATE TRIGGER records_ai AFTER INSERT ON records BEGIN INSERT INTO records_fts(rowid,memory_id,body) VALUES(new.rowid,new.memory_id,new.body); END;")
        conn.execute("INSERT INTO records_fts(records_fts) VALUES ('rebuild')")
        conn.commit()
    migrator = V1MemoryMigrator(tmp_path, include_managed=False)
    result = migrator.migrate()
    assert result.ok
    with migrator.memory_store._connection() as conn:
        outbox = [str(row[0]) for row in conn.execute("SELECT payload_json FROM domain_outbox")]
        # One authoritative decision row plus one evidence event per record;
        # FTS5 data/index/docsize/config rows must never become evidence.
        assert len(outbox) == len(rows) + 1
        assert all("records_fts" not in payload for payload in outbox)
        assert conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0] == len(rows)


def test_legacy_blob_columns_use_reversible_markers_and_replay_idempotently(tmp_path: Path):
    path = _v1_group(tmp_path, "blob", [_row("blob-memory", "text body")])
    blob = b"\\x00\\xfflegacy-bytes"
    with sqlite3.connect(path) as conn:
        # SQLite may legally return a BLOB from a TEXT column.  Auxiliary
        # legacy tables can carry the same shape, so exercise both paths.
        conn.execute("UPDATE records SET body=? WHERE memory_id=?", (blob, "blob-memory"))
        conn.execute("CREATE TABLE legacy_aux (id TEXT PRIMARY KEY, payload BLOB)")
        conn.execute("INSERT INTO legacy_aux VALUES (?, ?)", ("aux", blob))
        conn.commit()

    migrator = V1MemoryMigrator(tmp_path)
    first = migrator.migrate()
    second = migrator.migrate()

    assert first.ok and second.ok
    assert first.source_digest == second.source_digest
    atom = migrator.memory_store.get_atom(
        "blob-memory",
        scope={"share_group_id": "blob", "workspace_id": str(tmp_path)},
        include_building=True,
    )
    assert atom is not None
    marker = json.loads(atom.body)
    assert marker["__memoryguard_type__"] == "bytes"
    assert base64.b64decode(marker["base64"]) == blob
    assert "b'\\x00" not in atom.body
    with migrator.evidence_store._connection() as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_provenance_is_split_and_legacy_fallback_is_explicit(tmp_path: Path):
    provenance = json.dumps([{"source_object_id": "doc", "locator": "L3", "excerpt_hash": "digest-3", "source_revision": "r2"}])
    _v1_group(tmp_path, "g", [_row("with-prov", "a", provenance), _row("without-prov", "b")])
    migrator = V1MemoryMigrator(tmp_path)
    result = migrator.migrate()
    assert result.ok
    atoms = migrator.memory_store.list_atoms(scope={"share_group_id": "g", "workspace_id": str(tmp_path)}, include_building=True)
    with_prov = next(item for item in atoms if item.memory_id == "with-prov")
    evidence = migrator.evidence_store.list_for_subject("atom", with_prov.atom_id, scope={"workspace_id": str(tmp_path), "subject_type": "atom", "subject_id": with_prov.atom_id})
    assert evidence and evidence[0].source_ref.endswith("#provenance/doc/L3")
    assert evidence[0].digest == "digest-3"
    assert "body" not in evidence[0].metadata
    without = next(item for item in atoms if item.memory_id == "without-prov")
    fallback = migrator.evidence_store.list_for_subject("atom", without.atom_id, scope={"workspace_id": str(tmp_path), "subject_type": "atom", "subject_id": without.atom_id})
    assert fallback and fallback[0].authority == "legacy_record"


def test_building_atoms_are_hidden_until_promoted_and_tombstone_preserves_status(tmp_path: Path):
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    ctx = _ctx(tmp_path)
    atom = memory.put_atom(MemoryAtom(memory_id="x", body="body", share_group_id="g"), evidence=[{"source_ref": "g#x"}], context=ctx)
    assert memory.list_atoms() == []
    with pytest.raises(RuntimeError, match="outbox"):
        memory.promote("ready")
    memory.project_evidence(evidence)
    assert memory.validate(evidence).ok
    memory.promote("ready")
    assert memory.get_atom("x", scope={"share_group_id": "g", "workspace_id": str(tmp_path)}, include_building=True) is not None
    deleted = memory.delete("x", context=ctx, reason="test")
    assert deleted.status == "deleted"


def test_failed_evidence_projection_is_outstanding_and_retryable(tmp_path: Path):
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    ctx = _ctx(tmp_path)
    atom = memory.put_atom(
        MemoryAtom(memory_id="retry", body="body", share_group_id="g"),
        evidence=[{"source_ref": "g#retry"}],
        context=ctx,
    )

    class FlakyEvidence:
        def __init__(self):
            self.calls = 0

        def project_batch(self, events):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient sink failure")
            return evidence.project_batch(events)

    flaky = FlakyEvidence()
    first = memory.project_evidence(flaky)
    assert first == {"projected": 0, "failed": 1, "pending": 1}
    with memory._connection() as conn:
        row = conn.execute("SELECT status,attempts FROM domain_outbox WHERE aggregate_id=?", (atom.atom_id,)).fetchone()
        assert tuple(row) == ("failed", 1)
    assert memory.validate(evidence).ok is False
    with pytest.raises(RuntimeError, match="outstanding|evidence"):
        memory.promote("ready")

    second = memory.project_evidence(flaky)
    assert second == {"projected": 1, "failed": 0, "pending": 0}
    with memory._connection() as conn:
        row = conn.execute("SELECT status,attempts FROM domain_outbox WHERE aggregate_id=?", (atom.atom_id,)).fetchone()
        assert tuple(row) == ("projected", 2)
    assert memory.validate(evidence).ok is True
    memory.promote("ready")


def test_edits_use_revision_delta_ledger_and_replay_digest(tmp_path: Path):
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    ctx = _ctx(tmp_path)
    atom = memory.put_atom(MemoryAtom(memory_id="x", body="v0", share_group_id="g", visibility="ready"), evidence=[{"source_ref": "x"}], context=ctx)
    for index in range(100):
        atom.body = f"v{index + 1}"
        atom.canonical_hash = ""
        atom = memory.update_atom(atom, context=ctx)
    with memory._connection() as conn:  # schema-level assertion: one atom, append-only per-atom history
        assert conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM atom_revisions").fetchone()[0] == 101
        assert conn.execute("SELECT COUNT(*) FROM atom_deltas").fetchone()[0] == 100
        final_revision = conn.execute("SELECT MAX(revision) FROM atom_revisions").fetchone()[0]
    # Revision replay is a published read.  The initial write is building
    # until its evidence outbox is projected; keep the ledger assertions above
    # independent from publication, then replay only after that boundary.
    assert memory.project_evidence(evidence)["failed"] == 0
    replayed = memory.replay_revision(atom.atom_id, int(final_revision))
    assert replayed is not None
    assert memory.revision_digest(atom.atom_id, int(final_revision)) == memory.revision_digest(atom.atom_id)


def test_failure_rolls_back_one_group_and_source_bytes_are_unchanged(tmp_path: Path):
    path = _v1_group(tmp_path, "g", [_row("a"), _row("b")])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    migrator = V1MemoryMigrator(tmp_path, fail_at="g:record:1:after")
    with pytest.raises(RuntimeError):
        migrator.migrate()
    assert migrator.memory_store.status()["atoms"] == 0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_readonly_stores_do_not_create_or_mutate(tmp_path: Path):
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    ctx = _ctx(tmp_path)
    atom = memory.put_atom(MemoryAtom(memory_id="x", body="body", share_group_id="g"), evidence=[{"source_ref": "x"}], context=ctx)
    memory.project_evidence(evidence)
    before_m = memory.path.read_bytes()
    before_e = evidence.path.read_bytes()
    ro_m = MemoryAtomStore(memory.path, readonly=True)
    ro_e = EvidenceStore(evidence.path, readonly=True)
    assert ro_m.list_building_atoms(scope={"share_group_id": "g", "workspace_id": str(tmp_path)})
    assert ro_e.list_for_subject("atom", atom.atom_id, scope={"workspace_id": str(tmp_path), "subject_type": "atom", "subject_id": atom.atom_id})
    assert memory.path.read_bytes() == before_m
    assert evidence.path.read_bytes() == before_e


def test_public_reads_require_explicit_scope_and_evidence_rejects_attack_payloads(tmp_path: Path):
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    memory.put_atom(MemoryAtom(memory_id="same", body="one", share_group_id="g1"), evidence=[{"source_ref": "g1/same"}], context=_ctx(tmp_path, "g1"))
    memory.put_atom(MemoryAtom(memory_id="same", body="two", share_group_id="g2"), evidence=[{"source_ref": "g2/same"}], context=_ctx(tmp_path, "g2"))
    assert memory.list_atoms(include_building=True) == []
    assert memory.get_atom("same", include_building=True) is None
    assert [item.share_group_id for item in memory.list_atoms(scope={"share_group_id": "g1", "workspace_id": str(tmp_path)}, include_building=True)] == ["g1"]
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError, match="unknown evidence authority"):
        evidence.put_evidence(source_ref="attack", authority="caller-forged", context=ctx)
    with pytest.raises(ValueError, match="source body"):
        evidence.put_evidence(source_ref="attack", metadata={"nested": [{"text": "secret"}]}, context=ctx)
    with pytest.raises(ValueError, match="nesting"):
        evidence.put_evidence(source_ref="attack", metadata={"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {"i": 1}}}}}}}}}, context=ctx)
    with pytest.raises(ValueError, match="64 KiB"):
        evidence.put_evidence(source_ref="attack", metadata={"blob": "x" * (65 * 1024)}, context=ctx)


def test_store_rejects_outside_or_symlink_database_paths(tmp_path: Path):
    with pytest.raises((ValueError, OSError)):
        MemoryAtomStore(tmp_path / "outside.db")
    with pytest.raises((ValueError, OSError)):
        EvidenceStore(tmp_path / "outside.db")
    target = tmp_path / ".memoryguard" / "memory" / "memory.db"
    MemoryAtomStore(tmp_path)
    link = tmp_path / ".memoryguard" / "memory" / "link.db"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(Exception):
        MemoryAtomStore(link, readonly=True)


def test_evidence_id_is_identical_only_and_conflict_preserves_row(tmp_path: Path):
    evidence = EvidenceStore(tmp_path)
    ctx = _ctx(tmp_path)
    original = evidence.put_evidence(
        evidence_id="fixed-id",
        source_ref="source/a",
        revision="r1",
        digest="digest-a",
        authority="observed",
        metadata={"locator": "L1"},
        context=ctx,
    )
    evidence.link("fixed-id", "atom", "subject-fixed", context=ctx)
    before = evidence.path.read_bytes()
    replay = evidence.put_evidence(
        evidence_id="fixed-id",
        source_ref="source/a",
        revision="r1",
        digest="digest-a",
        authority="observed",
        metadata={"locator": "L1"},
        context=ctx,
    )
    assert replay.to_dict()["created_at"] == original.to_dict()["created_at"]
    with pytest.raises(ValueError, match="evidence_id conflict"):
        evidence.put_evidence(
            evidence_id="fixed-id",
            source_ref="source/a",
            revision="r1",
            digest="digest-a",
            authority="governance",
            metadata={"locator": "changed"},
            context=ctx,
        )
    assert evidence.path.read_bytes() == before
    assert evidence.get_evidence("fixed-id") is None
    authorized = evidence.get_evidence("fixed-id", scope={"workspace_id": str(tmp_path), "subject_type": "atom", "subject_id": "subject-fixed"})
    assert authorized is not None and authorized.to_dict()["authority"] == "observed"


def test_evidence_read_scope_is_subject_bound_and_migration_map_is_identical_only(tmp_path: Path):
    evidence = EvidenceStore(tmp_path)
    ctx = _ctx(tmp_path)
    item = evidence.put_evidence(evidence_id="scoped", source_ref="secret/ref", digest="d1", context=ctx)
    evidence.link(item.evidence_id, "atom", "allowed", context=ctx)
    assert evidence.get_evidence(item.evidence_id) is None
    assert evidence.get_evidence(item.evidence_id, scope={"workspace_id": str(tmp_path), "subject_type": "atom", "subject_id": "other"}) is None
    assert evidence.list_for_subject("atom", "allowed") == []
    scope = {"workspace_id": str(tmp_path), "subject_type": "atom", "subject_id": "allowed"}
    assert evidence.get_evidence(item.evidence_id, scope=scope) is not None
    assert evidence.list_for_subject("atom", "allowed", scope=scope)
    assert evidence.list_links_for_subject("atom", "allowed", scope=scope)
    map_id = evidence.record_migration_map("memory", "source", "row", "atom", "atom-1", metadata={"status": "migrated"})
    before = evidence.path.read_bytes()
    assert evidence.record_migration_map("memory", "source", "row", "atom", "atom-1", metadata={"status": "migrated"}) == map_id
    with pytest.raises(ValueError, match="migration map conflict"):
        evidence.record_migration_map("memory", "source", "row", "atom", "atom-1", metadata={"status": "changed"})
    assert evidence.path.read_bytes() == before


def test_memory_mutations_require_workspace_group_scope_and_private_capability(tmp_path: Path):
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    ctx = _ctx(tmp_path)
    old = memory.put_atom(MemoryAtom(memory_id="old", body="old", share_group_id="g"), evidence=[{"source_ref": "old"}], context=ctx)
    new = memory.put_atom(MemoryAtom(memory_id="new", body="new", share_group_id="g"), evidence=[{"source_ref": "new"}], context=ctx)
    memory.project_evidence(evidence)
    with pytest.raises(PermissionError, match="mutation scope"):
        memory.delete("old", share_group_id="g")
    with pytest.raises(PermissionError, match="mutation scope"):
        memory.supersede(old.atom_id, new.atom_id, share_group_id="g")
    with pytest.raises(PermissionError, match="workspace"):
        memory.delete("old", scope={"workspace_id": str(tmp_path / "other"), "share_group_id": "g"})
    with pytest.raises(PermissionError, match="capability"):
        memory._supersede_for_migration(old.atom_id, new.atom_id, share_group_id="g", capability=object())
    memory.supersede(old.atom_id, new.atom_id, context=ctx, reason="test")
    assert memory.get_atom("old", scope={"workspace_id": str(tmp_path), "share_group_id": "g"}, include_building=True).status == "superseded"


def test_atom_provenance_and_scope_workspace_are_fail_closed(tmp_path: Path):
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError, match="source body"):
        memory.put_atom(
            MemoryAtom(memory_id="bad", body="body", share_group_id="g", provenance=[{"nested": {"body": "raw"}}]),
            evidence=[{"source_ref": "g/bad"}],
            context=ctx,
        )
    memory.put_atom(MemoryAtom(memory_id="ok", body="body", share_group_id="g"), evidence=[{"source_ref": "g/ok"}], context=ctx)
    with pytest.raises(PermissionError, match="workspace_id"):
        memory.list_atoms(scope={"workspace_id": str(tmp_path / "other"), "share_group_id": "g"}, include_building=True)
    assert memory.list_atoms(scope={"workspace_id": str(tmp_path), "share_group_id": "g"}, include_building=True)
