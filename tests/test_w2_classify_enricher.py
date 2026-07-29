from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.auto_organizer import AutoOrganizer
from memoryguard.memory_ir import MemoryNormalizer, _infer_kind
from memoryguard.policies import CommunityPolicy, classify_kind
from memoryguard.schema_v3 import (
    CoverageLedger,
    MemoryKind,
    SourceObject,
    SourceSnapshot,
    stable_hash,
)
from memoryguard.semantic_enricher import HeuristicEnricher, PassthroughEnricher, get_enricher


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


def _snapshot_for(root_id: str, relative_path: str, content: str) -> SourceSnapshot:
    obj = SourceObject(
        source_object_id=stable_hash(root_id, relative_path),
        source_root_id=root_id,
        relative_path=relative_path,
        content_hash=stable_hash(content),
    )
    return SourceSnapshot(
        snapshot_id="snap",
        created_at="",
        source_objects=[obj],
        coverage=CoverageLedger(),
    )


def test_classify_unified_ir_and_policy_agree(tmp_path) -> None:
    policy = CommunityPolicy()
    organizer = AutoOrganizer(str(tmp_path), "default")

    for content, expected in _CLASSIFY_SAMPLES:
        policy_kind = policy.classify(content, {})
        classify_kind_result = classify_kind(content)
        ir_kind = _infer_kind("", content).value
        organizer_kind = organizer._classify(content).value

        assert policy_kind == expected, content
        assert classify_kind_result == expected, content
        assert ir_kind == expected, content
        assert organizer_kind == expected, content
        assert policy_kind == classify_kind_result == ir_kind == organizer_kind


def test_heuristic_enricher_sets_localization_mode() -> None:
    enricher = HeuristicEnricher()
    result = enricher.enrich(
        title="Prefer concise commit messages",
        body="Avoid AI-sounding summaries in commit messages.",
    )

    assert result.kind == "preference"
    assert result.localization_mode == "heuristic"
    assert result.display_language == "mixed"
    assert result.enrichment_mode == "heuristic"
    assert result.title.startswith("偏好：")
    assert "中文整理：" not in result.body


def test_enricher_off_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYGUARD_ENRICHER", "off")
    enricher = get_enricher()
    assert isinstance(enricher, PassthroughEnricher)

    result = enricher.enrich(
        title="Prefer concise commit messages",
        body="Avoid AI-sounding summaries.",
    )

    assert result.enrichment_mode == "passthrough"
    assert result.localization_mode == "none"
    assert result.title == "Prefer concise commit messages"
    assert result.body == "Avoid AI-sounding summaries."
    assert result.kind == "preference"


def test_memory_normalizer_uses_enricher_by_default(tmp_path) -> None:
    content = (
        "Prefer concise commit messages.\n\n"
        "Avoid AI-sounding summaries in commit messages."
    )
    notes = tmp_path / "notes.md"
    notes.write_text(content, encoding="utf-8")
    snapshot = _snapshot_for("root-1", "notes.md", content)

    ir = MemoryNormalizer(tmp_path).normalize(
        snapshot,
        root_map={"root-1": str(tmp_path)},
    )

    assert len(ir.records) == 1
    rec = ir.records[0]
    assert rec.kind == MemoryKind.PREFERENCE
    assert rec.localization_mode == "heuristic"
    assert rec.original_body.startswith("Prefer concise")
    assert rec.title.startswith("偏好：")


def test_memory_normalizer_passthrough_mode(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORYGUARD_ENRICHER", "off")
    content = "Prefer concise commit messages."
    notes = tmp_path / "notes.md"
    notes.write_text(content, encoding="utf-8")
    snapshot = _snapshot_for("root-1", "notes.md", content)

    ir = MemoryNormalizer(tmp_path).normalize(
        snapshot,
        root_map={"root-1": str(tmp_path)},
    )

    assert len(ir.records) == 1
    rec = ir.records[0]
    assert rec.localization_mode == "none"
    assert rec.title == "Prefer concise commit messages."
    assert rec.body == "Prefer concise commit messages."
