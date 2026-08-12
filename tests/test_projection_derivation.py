"""Reference-only derivation assertions for the V2 projection service."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from _publish_helpers import build_projection, projection_scope, seed_atom
from memoryguard.projection_v2 import ProjectionStore
from memoryguard.runtime_v2.projection_build import ProjectionBuildService


def _provenance(source: str, locator: str = "heading:a") -> list[Mapping[str, Any]]:
    return [{"source_object_id": source, "locator": locator, "excerpt_hash": "h"}]


def _metadata(tmp_path: Path, atoms: list[Mapping[str, Any]]) -> dict[str, Any]:
    for item in atoms:
        seed_atom(
            tmp_path,
            str(item["memory_id"]),
            str(item.get("body") or ""),
            kind=str(item.get("kind") or "fact"),
            metadata=item.get("metadata"),
            provenance=item.get("provenance"),
        )
    scope = projection_scope(tmp_path)
    result = build_projection(tmp_path, scope=scope)
    assert result["status"] == "succeeded"
    service = ProjectionBuildService(tmp_path)
    key = service._scope_key("reconstructed", scope)
    record = ProjectionStore(tmp_path, initialize=False).get_projection(
        "scenario", key, scope=scope
    )
    assert record is not None
    return dict(record.payload["metadata"])


def test_projection_builds_source_hub_and_related_edges(tmp_path: Path) -> None:
    metadata = _metadata(tmp_path, [
        {
            "memory_id": "aaaaaaaaaaaaaaaa",
            "kind": "fact",
            "body": "同一文件事实一",
            "provenance": _provenance("src-file-1"),
            "metadata": {
                "title": "事实A",
                "scope": "project",
                "duplicate_group": "dup-1",
                "duplicate_decision": "keep_all",
            },
        },
        {
            "memory_id": "bbbbbbbbbbbbbbbb",
            "kind": "fact",
            "body": "同一文件事实二",
            "provenance": _provenance("src-file-1", "heading:b"),
            "metadata": {"title": "事实B", "scope": "project"},
        },
        {
            "memory_id": "cccccccccccccccc",
            "kind": "preference",
            "body": "另一来源偏好",
            "provenance": _provenance("src-file-2"),
            "metadata": {"title": "偏好C", "scope": "project"},
        },
        {
            "memory_id": "dddddddddddddddd",
            "kind": "fact",
            "body": "独立事实",
            "provenance": _provenance("src-file-3"),
            "metadata": {
                "title": "事实D",
                "scope": "project",
                "duplicate_group": "dup-1",
                "duplicate_decision": "keep_all",
            },
        },
    ])
    graph = metadata["derived_graph"]
    nodes = graph["nodes"]
    kinds = {node["id"]: node["node_kind"] for node in nodes}
    assert any(kind == "root" for kind in kinds.values())
    assert any(kind == "topic" for kind in kinds.values())
    assert any(kind == "source_hub" for kind in kinds.values()), "同文件 ≥2 条应生成同源突触"
    assert sum(1 for kind in kinds.values() if kind == "claim_anchor") == 4

    hub = next(node for node in nodes if node["node_kind"] == "source_hub")
    assert hub["source_key"] == "src-file-1"
    assert len(hub["member_ids"]) == 2
    assert "记忆胞体" in hub["derivation"]

    edge_types = {edge["edge_type"] for edge in graph["edges"]}
    assert "derived_from" in edge_types
    assert "related" in edge_types  # KEEP_ALL → related
    assert metadata["llm_used"] is False
    assert metadata["derivation_engine"] == "deterministic_v3"
    stats = metadata["derived_stats"]
    assert stats["source_hub_count"] >= 1
    assert stats["related_edge_count"] >= 1


def test_projection_topic_labels_are_chinese(tmp_path: Path) -> None:
    metadata = _metadata(tmp_path, [{
        "memory_id": "eeeeeeeeeeeeeeee",
        "kind": "preference",
        "body": "prefer short",
        "provenance": _provenance("s1"),
        "metadata": {"title": "喜欢简洁", "scope": "project"},
    }])
    nodes = metadata["derived_graph"]["nodes"]
    topics = [node for node in nodes if node["node_kind"] == "topic"]
    labels = {node["label"] for node in topics}
    assert "项目来源" in labels
    assert "偏好" in labels
    scope = next(node for node in topics if node["label"] == "项目来源")
    kind_topic = next(node for node in topics if node["label"] == "偏好")
    assert scope["parent_id"] == "main"
    assert kind_topic["parent_id"] == scope["id"]
    root = next(node for node in nodes if node["node_kind"] == "root")
    assert root["label"] == "记忆胞体"
    assert metadata["derivation_engine"] == "deterministic_v3"


def test_projection_splits_user_and_project_scope(tmp_path: Path) -> None:
    metadata = _metadata(tmp_path, [
        {
            "memory_id": "ffffffffffffffff",
            "kind": "fact",
            "body": "user fact",
            "provenance": _provenance("s-user"),
            "metadata": {"title": "用户事实", "scope": "user"},
        },
        {
            "memory_id": "gggggggggggggggg",
            "kind": "fact",
            "body": "project fact",
            "provenance": _provenance("s-proj"),
            "metadata": {"title": "项目事实", "scope": "project"},
        },
    ])
    nodes = metadata["derived_graph"]["nodes"]
    scope_topics = [
        node for node in nodes
        if node["node_kind"] == "topic" and node["parent_id"] == "main"
    ]
    assert {node["label"] for node in scope_topics} == {"项目来源", "用户来源"}
