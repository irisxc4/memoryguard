from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.memory_ir import MemoryIR, MemoryNormalizer
from memoryguard.schema_v3 import CoverageLedger, MemoryKind, MemoryRecord, Provenance, SourceObject, SourceSnapshot, stable_hash


def _snapshot_for(root_id: str, relative_path: str, content: str) -> SourceSnapshot:
    obj = SourceObject(
        source_object_id=stable_hash(root_id, relative_path),
        source_root_id=root_id,
        relative_path=relative_path,
        content_hash=stable_hash(content),
    )
    return SourceSnapshot(snapshot_id="snap", created_at="", source_objects=[obj], coverage=CoverageLedger())


def test_conversation_history_sources_are_not_normalized_into_long_term_memory(tmp_path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    content = "User: remember that I prefer short answers\nAssistant: noted"
    (root / "session.jsonl").write_text(content, encoding="utf-8")
    snapshot = _snapshot_for("conversation-root", "session.jsonl", content)

    ir = MemoryNormalizer(tmp_path).normalize(
        snapshot,
        root_map={"conversation-root": str(root)},
        root_policies={"conversation-root": {"source_category": "conversation_history", "ingestion_policy": "evidence_only"}},
    )

    assert ir.records == []


def test_policy_filter_removes_existing_conversation_records_from_legacy_ir(tmp_path) -> None:
    snapshot = _snapshot_for("conversation-root", "session.jsonl", "content")
    record = MemoryRecord(
        memory_id="m1",
        kind=MemoryKind.FACT,
        title="旧会话记忆",
        body="旧会话上下文不应进入长期记忆",
        provenance=[Provenance(source_object_id=snapshot.source_objects[0].source_object_id, locator="line:1", excerpt_hash="h")],
    )
    ir = MemoryIR(records=[record], snapshot_id="snap")

    changed = MemoryNormalizer(tmp_path).filter_by_source_policies(
        ir,
        snapshot,
        {"conversation-root": {"source_category": "conversation_history", "ingestion_policy": "evidence_only"}},
    )

    assert changed is True
    assert ir.records == []
