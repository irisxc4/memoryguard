"""神经图衍生逻辑：胞体 → 主题 → 同源突触 → 末梢；确定性，不调 LLM。"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.memory_ir import MemoryIR
from memoryguard.projection import ProjectionBuilder
from memoryguard.schema_v3 import (
    Completeness, DuplicateDecision, DuplicateGroup, MemoryKind, MemoryRecord,
    MemoryStatus, Provenance,
)


def _rec(mid: str, kind: MemoryKind, title: str, body: str, source: str, locator: str = "heading:a") -> MemoryRecord:
    return MemoryRecord(
        memory_id=mid,
        kind=kind,
        title=title,
        body=body,
        status=MemoryStatus.CANDIDATE,
        completeness=Completeness.VERIFIABLE,
        confidence=0.8,
        provenance=[Provenance(source_object_id=source, locator=locator, excerpt_hash="h")],
    )


def test_projection_builds_source_hub_and_related_edges(tmp_path) -> None:
    ir = MemoryIR(
        snapshot_id="snap-derivation",
        records=[
            _rec("aaaaaaaaaaaaaaaa", MemoryKind.FACT, "事实A", "同一文件事实一", "src-file-1"),
            _rec("bbbbbbbbbbbbbbbb", MemoryKind.FACT, "事实B", "同一文件事实二", "src-file-1"),
            _rec("cccccccccccccccc", MemoryKind.PREFERENCE, "偏好C", "另一来源偏好", "src-file-2"),
            _rec("dddddddddddddddd", MemoryKind.FACT, "事实D", "独立事实", "src-file-3"),
        ],
        duplicate_groups=[
            DuplicateGroup(
                group_id="dup-1",
                member_ids=["aaaaaaaaaaaaaaaa", "dddddddddddddddd"],
                decision=DuplicateDecision.KEEP_ALL,
            ),
        ],
    )
    proj = ProjectionBuilder(tmp_path, "reconstructed").build(ir)

    kinds = {n.id: n.node_kind for n in proj.nodes}
    assert any(k == "root" for k in kinds.values())
    assert any(k == "topic" for k in kinds.values())
    assert any(k == "source_hub" for k in kinds.values()), "同文件 ≥2 条应生成同源突触"
    assert sum(1 for k in kinds.values() if k == "claim_anchor") == 4

    hub = next(n for n in proj.nodes if n.node_kind == "source_hub")
    assert hub.source_key == "src-file-1"
    assert len(hub.member_ids) == 2
    assert "记忆胞体" in hub.derivation

    edge_types = {e.edge_type for e in proj.edges}
    assert "derived_from" in edge_types
    assert "related" in edge_types  # KEEP_ALL → related
    assert proj.meta.get("llm_used") is False
    assert proj.meta.get("derivation_engine") == "deterministic_v3"
    stats = proj.to_dict()["stats"]
    assert stats["source_hub_count"] >= 1
    assert stats["related_edge_count"] >= 1


def test_projection_topic_labels_are_chinese(tmp_path) -> None:
    ir = MemoryIR(
        snapshot_id="snap-zh",
        records=[_rec("eeeeeeeeeeeeeeee", MemoryKind.PREFERENCE, "喜欢简洁", "prefer short", "s1")],
    )
    proj = ProjectionBuilder(tmp_path).build(ir)
    topics = [n for n in proj.nodes if n.node_kind == "topic"]
    labels = {n.label for n in topics}
    assert "项目来源" in labels
    assert "偏好" in labels
    scope = next(n for n in topics if n.label == "项目来源")
    kind_topic = next(n for n in topics if n.label == "偏好")
    assert scope.parent_id == "main"
    assert kind_topic.parent_id == scope.id
    root = next(n for n in proj.nodes if n.node_kind == "root")
    assert root.label == "记忆胞体"
    assert proj.meta.get("derivation_engine") == "deterministic_v3"


def test_projection_splits_user_and_project_scope(tmp_path) -> None:
    user = _rec("ffffffffffffffff", MemoryKind.FACT, "用户事实", "user fact", "s-user")
    user.scope = "user"
    proj_rec = _rec("gggggggggggggggg", MemoryKind.FACT, "项目事实", "project fact", "s-proj")
    ir = MemoryIR(snapshot_id="snap-scope", records=[user, proj_rec])
    proj = ProjectionBuilder(tmp_path).build(ir)
    scope_topics = [n for n in proj.nodes if n.node_kind == "topic" and n.parent_id == "main"]
    assert {n.label for n in scope_topics} == {"项目来源", "用户来源"}
