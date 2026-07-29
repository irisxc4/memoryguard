import copy
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.distiller import MemoryDistiller
from memoryguard.gui import GovernanceApi
from _publish_helpers import prepare_publish_target, publish
from memoryguard.memory_ir import MemoryIR, MemoryNormalizer
from memoryguard.schema_v3 import (
    DuplicateGroup,
    MemoryKind,
    MemoryRecord,
    MemoryStatus,
    Provenance,
)


def _similar_pair_ir() -> MemoryIR:
    rec_a = MemoryRecord(
        memory_id="m-a",
        kind=MemoryKind.PREFERENCE,
        title="偏好简洁提交信息",
        body="偏好使用简洁的 commit message，避免 AI 风格的冗长摘要。",
        confidence=0.7,
        provenance=[Provenance("src-1", "line:1-3", "hash-a")],
    )
    rec_b = MemoryRecord(
        memory_id="m-b",
        kind=MemoryKind.PREFERENCE,
        title="偏好简洁提交",
        body="偏好使用简洁 commit message，避免 AI 风格冗长摘要说明。",
        confidence=0.6,
        provenance=[Provenance("src-1", "line:4-6", "hash-b")],
    )
    return MemoryIR(
        records=[rec_a, rec_b],
        duplicate_groups=[DuplicateGroup(
            group_id="dup-1",
            member_ids=["m-a", "m-b"],
            similarity_method="tfidf_cosine",
            scores=[0.92],
        )],
        snapshot_id="snap-dup",
    )


def test_distill_merges_duplicate_group(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ir = _similar_pair_ir()
    original_len = len(ir.records)

    distilled = MemoryDistiller(workspace).distill(ir)

    assert distilled.stats["input_count"] == 2
    assert distilled.stats["output_count"] == 1
    assert distilled.stats["output_count"] < distilled.stats["input_count"]
    assert len(distilled.groups) == 1
    assert set(distilled.redundant_record_ids) == {"m-b"}
    assert set(distilled.groups[0].source_record_ids) == {"m-a", "m-b"}
    assert len(ir.records) == original_len


def test_distill_does_not_mutate_ir_records(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ir = _similar_pair_ir()
    before = [copy.deepcopy(r.to_dict()) for r in ir.records]

    MemoryDistiller(workspace).distill(ir)

    after = [r.to_dict() for r in ir.records]
    assert after == before


def test_publish_uses_distilled_by_default(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "native"
    workspace.mkdir()
    target_dir.mkdir()
    target = target_dir / "memory.md"
    target.write_text("# Memory\n\n旧内容\n", encoding="utf-8")

    ir = _similar_pair_ir()
    MemoryNormalizer(workspace).save(ir)

    api, root_id, scope = prepare_publish_target(workspace, target, ir)
    published = publish(api, scope=scope, target_root_id=root_id)
    written = target.read_text(encoding="utf-8")
    section_count = written.count("\n## ")

    assert published["ok"] is True
    assert published.get("distilled") is True
    assert published.get("distill_stats", {}).get("output_count") == 1
    assert section_count == 1
    assert (workspace / ".memoryguard" / "ir" / "distilled.json").exists()


def test_publish_raw_fallback(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "native"
    workspace.mkdir()
    target_dir.mkdir()
    target = target_dir / "memory.md"
    target.write_text("# Memory\n\n旧内容\n", encoding="utf-8")

    ir = _similar_pair_ir()
    MemoryNormalizer(workspace).save(ir)
    api, root_id, scope = prepare_publish_target(workspace, target, ir)

    raw_published = publish(api, scope=scope, target_root_id=root_id, use_distilled=False)
    raw_written = target.read_text(encoding="utf-8")
    assert raw_published.get("distilled") is False
    assert "distill_stats" not in raw_published
    assert raw_written.count("\n## ") == 2

    monkeypatch.setenv("MEMORYGUARD_PUBLISH_RAW", "1")
    env_published = publish(api, scope=scope, target_root_id=root_id)
    env_written = target.read_text(encoding="utf-8")

    assert env_published.get("distilled") is False
    assert env_written.count("\n## ") == 2


def test_distill_and_publish_skips_superseded(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target_dir = tmp_path / "native"
    workspace.mkdir()
    target_dir.mkdir()
    target = target_dir / "memory.md"
    target.write_text("# Memory\n\n旧内容\n", encoding="utf-8")

    active = MemoryRecord(
        memory_id="m-active",
        kind=MemoryKind.FACT,
        title="当前有效事实",
        body="这条应该被发布。",
        status=MemoryStatus.CANDIDATE,
    )
    superseded = MemoryRecord(
        memory_id="m-old",
        kind=MemoryKind.FACT,
        title="已被取代的旧事实",
        body="这条 superseded 正文不应出现在目标文件。",
        status=MemoryStatus.SUPERSEDED,
    )
    ir = MemoryIR(records=[active, superseded], snapshot_id="snap-sup")
    MemoryNormalizer(workspace).save(ir)

    api, root_id, scope = prepare_publish_target(workspace, target, ir)
    published = publish(api, scope=scope, target_root_id=root_id)
    written = target.read_text(encoding="utf-8")

    assert published["ok"] is True
    assert "当前有效事实" in written
    assert "这条应该被发布" in written
    assert "已被取代的旧事实" not in written
    assert "这条 superseded 正文不应出现在目标文件" not in written
