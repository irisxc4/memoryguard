from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

from memoryguard.memory.store import MemoryAtom, stable_digest
from memoryguard.projection_v2 import ProjectionReadScope
from memoryguard.runtime_v2.projection_build import ProjectionBuildService


def test_large_evidence_projection_compacts_payload_without_losing_links(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = str(tmp_path.resolve())
    scope = ProjectionReadScope(
        workspace_id=workspace,
        share_group_id="group-a",
    )
    atom = MemoryAtom(
        atom_id="atom-large-evidence",
        memory_id="memory-large-evidence",
        body="stable projected memory",
        kind="fact",
        confidence=0.9,
        canonical_hash=stable_digest("stable projected memory"),
        workspace_id=workspace,
        share_group_id="group-a",
        metadata={"scope": "shared", "title": "large evidence fixture"},
    )
    rows = [
        SimpleNamespace(
            evidence_id=f"evidence-{index:04d}-" + ("x" * 24),
            digest=(f"{index:064x}")[-64:],
            status="valid",
        )
        for index in range(600)
    ]

    class FakeEvidenceStore:
        def list_for_subject(self, *args, **kwargs):
            return rows

    service = ProjectionBuildService(tmp_path)
    monkeypatch.setattr(service, "_memory", lambda: object())
    monkeypatch.setattr(
        service,
        "_scoped_atoms",
        lambda memory, selected_scope, runtime_role="": [atom],
    )
    monkeypatch.setattr(service, "_evidence", lambda: FakeEvidenceStore())

    result = service.build(mode="reconstructed", scope=scope)
    assert result["status"] == "succeeded"
    assert result["evidence_count"] == 600

    store = service._projection(write=False)
    key = service._scope_key("reconstructed", scope)
    record = store.get_projection("scenario", key, scope=scope)
    assert record is not None
    metadata = dict(record.payload.get("metadata") or {})
    assert metadata["evidence_refs_compacted"] is True
    assert metadata["evidence_refs_storage"] == "projection_evidence_links"
    assert record.payload.get("evidence_refs") == []
    assert len(record.evidence_links) == 600

    with store.connection("scenario") as connection:
        evidence_count = connection.execute(
            "SELECT COUNT(*) FROM projection_evidence_links WHERE projection_id=?",
            (record.projection_id,),
        ).fetchone()[0]
        item_count = connection.execute(
            "SELECT COUNT(*) FROM projection_items WHERE projection_id=?",
            (record.projection_id,),
        ).fetchone()[0]
    assert evidence_count == 600
    assert item_count == 600
