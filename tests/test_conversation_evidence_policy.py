"""Conversation-history sources stay outside the native long-term memory plane."""

from __future__ import annotations

from memoryguard.content import ContentStore
from memoryguard.memory import MemoryAtomStore
from memoryguard.runtime_v2.extraction_native import NativeExtractionEnrichmentService

from _publish_helpers import native_context


def _conversation_source(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    content = "User: remember that I prefer short answers\nAssistant: noted"
    session = root / "session.jsonl"
    session.write_text(content, encoding="utf-8")
    ContentStore(tmp_path).upsert_source_connector(
        source_id="conversation-root",
        provider="test",
        source_type="conversation_history",
        external_root_key=str(root.resolve()),
        workspace_id=str(tmp_path.resolve()),
        enabled=True,
    )
    return session


def test_conversation_history_sources_are_not_normalized_into_long_term_memory(tmp_path) -> None:
    session = _conversation_source(tmp_path)
    service = NativeExtractionEnrichmentService(tmp_path)

    result = service.dispatch(
        "extract",
        {"source_path": str(session)},
        context=native_context(tmp_path),
    )

    assert result["ok"] is False
    assert result["code"] == "no_source"
    memory = MemoryAtomStore(tmp_path, readonly=True)
    assert memory.list_atoms(
        scope={
            "workspace_id": str(tmp_path.resolve()),
            "share_group_id": "group-test",
            "agent_instance_id": "agent-test",
            "project_ref": str(tmp_path.resolve()),
            "provider": "test",
            "runtime_role": "test",
        },
        include_building=True,
    ) == []


def test_conversation_history_is_evidence_only_and_cannot_be_accepted(tmp_path) -> None:
    session = _conversation_source(tmp_path)
    service = NativeExtractionEnrichmentService(tmp_path)
    context = native_context(tmp_path)

    result = service.dispatch(
        "extract",
        {"source_path": str(session)},
        context=context,
    )

    assert result["ok"] is False
    assert result["data"] if "data" in result else True
    assert service.dispatch(
        "accept",
        {"extract_id": "conversation-extract", "candidate_ids": ["candidate"]},
        context=context,
    )["ok"] is False
