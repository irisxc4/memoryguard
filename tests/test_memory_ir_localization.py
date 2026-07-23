from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.memory_ir import MemoryIR, MemoryNormalizer
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
    assert ir.records[0].body.startswith("中文整理：")
    assert ir.records[0].original_title == "Prefer compact project memory rules"
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
