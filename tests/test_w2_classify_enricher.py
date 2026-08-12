"""W2 classification and native extraction/localization coverage."""

from __future__ import annotations

from pathlib import Path

import pytest

from _publish_helpers import mutation_context, native_context
from memoryguard.content import ContentStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory import MemoryAtomStore
from memoryguard.policies import CommunityPolicy, classify_kind
from memoryguard.runtime_v2.extraction_native import NativeExtractionEnrichmentService
from memoryguard.runtime_v2.organizer import V2MemoryOrganizer
from memoryguard.runtime_v2.text_native import classify_kind as native_classify_kind
from memoryguard.runtime_v2.text_native import localize_native_text


_CLASSIFY_SAMPLES = [
    ("I prefer concise commit messages", "preference"),
    ("Follow these steps to deploy", "procedure"),
    ("This project uses a monorepo", "project"),
    ("An episode happened yesterday", "episode"),
    ("Actually that was wrong, it should be X", "correction"),
    ("The sky is blue on clear days", "fact"),
    ("偏好使用简洁的提交信息", "preference"),
    ("纠正：之前的说法不对", "correction"),
]


def _organizer(workspace: Path) -> V2MemoryOrganizer:
    memory = MemoryAtomStore(workspace, readonly=False)
    return V2MemoryOrganizer(
        workspace,
        "group-test",
        memory_store=memory,
        governance=GovernanceV2(workspace, memory_store=memory),
    )


def _extract_one(tmp_path: Path, content: str):
    source = tmp_path / "notes.md"
    source.write_text(content, encoding="utf-8")
    ContentStore(tmp_path).upsert_source_connector(
        source_id="selected-notes",
        provider="test",
        source_type="selected_file",
        external_root_key=str(source.resolve()),
        workspace_id=str(tmp_path.resolve()),
        enabled=True,
    )
    service = NativeExtractionEnrichmentService(tmp_path)
    context = native_context(tmp_path)
    preview = service.extract({"source_path": str(source)}, context=context)
    assert preview["candidates"]
    return service, context, preview


def test_classify_native_organizer_and_policy_agree(tmp_path: Path) -> None:
    policy = CommunityPolicy()
    organizer = _organizer(tmp_path)
    context = mutation_context(tmp_path)

    for index, (content, expected) in enumerate(_CLASSIFY_SAMPLES):
        result = organizer.write(
            {
                "event_id": f"classify-{index}",
                "body": content,
                "agent_instance_id": "agent-test",
                "share_group_id": "group-test",
                "project_ref": str(tmp_path.resolve()),
                "provider": "test",
                "runtime_role": "test",
                "visibility": "active",
            },
            context=context,
        )

        assert policy.classify(content, {}) == expected
        assert classify_kind(content) == expected
        assert native_classify_kind(content) == expected
        assert result["atom"].kind == expected


def test_native_localizer_sets_heuristic_mode() -> None:
    result = localize_native_text(
        "Prefer concise commit messages",
        "Avoid AI-sounding summaries in commit messages.",
        "preference",
    )

    assert result["localization_mode"] == "heuristic"
    assert result["display_language"] == "mixed"
    assert result["title"].startswith("偏好：")
    assert "中文整理：" not in result["body"]


def test_native_localizer_passthrough_for_chinese_text() -> None:
    result = localize_native_text("中文标题", "中文正文", "fact")

    assert result["localization_mode"] == "none"
    assert result["display_language"] == "zh"
    assert result["title"] == "中文标题"
    assert result["body"] == "中文正文"


def test_native_extraction_uses_native_classifier_and_localizer(tmp_path: Path) -> None:
    service, context, preview = _extract_one(
        tmp_path,
        "# Preference\n\nPrefer concise commit messages.\n\nAvoid AI-sounding summaries in commit messages.",
    )
    candidate = preview["candidates"][0]
    localized = localize_native_text("", candidate["preview"], candidate["kind"])
    accepted = service.accept(
        {"extract_id": preview["extract_id"], "candidate_ids": [candidate["candidate_id"]]},
        context=context,
    )

    assert candidate["kind"] == "preference"
    assert localized["localization_mode"] == "heuristic"
    assert localized["title"].startswith("偏好：")
    assert accepted["accepted"][0]["kind"] == "preference"


def test_native_extraction_keeps_chinese_text_without_localization(tmp_path: Path) -> None:
    service, context, preview = _extract_one(tmp_path, "# 中文标题\n\n这是一条中文记忆正文")
    candidate = preview["candidates"][0]
    localized = localize_native_text("中文标题", candidate["preview"], candidate["kind"])
    accepted = service.accept(
        {"extract_id": preview["extract_id"], "candidate_ids": [candidate["candidate_id"]]},
        context=context,
    )
    memory = MemoryAtomStore(tmp_path, readonly=True)
    atom = memory.get_atom(
        accepted["accepted"][0]["memory_id"],
        scope={
            "workspace_id": str(tmp_path.resolve()),
            "share_group_id": "group-test",
            "agent_instance_id": "agent-test",
            "project_ref": str(tmp_path.resolve()),
            "provider": "test",
            "runtime_role": "test",
        },
        include_building=True,
    )

    assert localized["localization_mode"] == "none"
    assert localized["body"] == candidate["preview"]
    assert atom is not None
    assert "中文记忆正文" in atom.body
