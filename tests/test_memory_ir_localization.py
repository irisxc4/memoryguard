from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.memory_ir import MemoryIR, MemoryNormalizer, localize_memory_text
from memoryguard.schema_v3 import MemoryKind, MemoryRecord


def test_memory_normalizer_localizes_existing_english_ir_records(tmp_path) -> None:
    rec = MemoryRecord(
        memory_id="m1",
        kind=MemoryKind.PREFERENCE,
        title="Prefer compact project memory rules",
        body="The agent should use project memory files as the source of truth and avoid unrelated global preferences.",
    )
    ir = MemoryIR(records=[rec], snapshot_id="s")

    changed = MemoryNormalizer(tmp_path).ensure_localized(ir)

    assert changed is True
    assert ir.records[0].title.startswith("偏好：")
    assert "中文整理：" not in ir.records[0].body
    assert ir.records[0].localization_mode == "heuristic"
    assert ir.records[0].display_language == "mixed"
    assert ir.records[0].original_title == "Prefer compact project memory rules"
    assert ir.records[0].original_body.startswith("The agent should use")


def test_localize_marks_heuristic_not_translated() -> None:
    title, body, original_title, original_body, display_language, localization_mode = localize_memory_text(
        "Prefer concise commit messages",
        "Avoid AI-sounding summaries in commit messages.",
        MemoryKind.PREFERENCE,
    )

    assert localization_mode == "heuristic"
    assert display_language == "mixed"
    assert "中文整理：" not in body
    assert original_title == "Prefer concise commit messages"
    assert original_body.startswith("Avoid AI-sounding")


def test_ensure_localized_migrates_legacy_fake_chinese_prefix(tmp_path) -> None:
    rec = MemoryRecord(
        memory_id="m-legacy",
        kind=MemoryKind.PREFERENCE,
        title="偏好：Prefer compact project memory rules",
        body="中文整理：The agent should use project memory files as the source of truth.",
        original_title="Prefer compact project memory rules",
        original_body="The agent should use project memory files as the source of truth.",
        localization_mode="none",
    )
    ir = MemoryIR(records=[rec], snapshot_id="s")

    changed = MemoryNormalizer(tmp_path).ensure_localized(ir)

    assert changed is True
    assert "中文整理：" not in ir.records[0].body
    assert "中文辅助摘要：" not in ir.records[0].body
    assert ir.records[0].localization_mode == "heuristic"
    assert ir.records[0].original_body.startswith("The agent should use")


def test_memory_normalizer_keeps_chinese_ir_body_as_default(tmp_path) -> None:
    rec = MemoryRecord(
        memory_id="m2",
        kind=MemoryKind.FACT,
        title="中文标题",
        body="这是一条中文记忆正文",
    )
    ir = MemoryIR(records=[rec], snapshot_id="s")

    changed = MemoryNormalizer(tmp_path).ensure_localized(ir)

    assert changed is False
    assert ir.records[0].title == "中文标题"
    assert ir.records[0].body == "这是一条中文记忆正文"
    assert ir.records[0].localization_mode == "none"
