from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.gui import GovernanceApi


def test_get_neuron_graph_hydrates_legacy_projection_nodes_from_ir(tmp_path) -> None:
    ir_dir = tmp_path / ".memoryguard" / "ir"
    proj_dir = tmp_path / ".memoryguard" / "projections"
    ir_dir.mkdir(parents=True)
    proj_dir.mkdir(parents=True)
    memory_id = "a" * 16
    related_id = "b" * 16
    (ir_dir / "current.json").write_text(json.dumps({
        "records": [
            {"memory_id": memory_id, "kind": "fact", "title": "正文标题", "body": "这是一段真实正文", "scope": "project", "confidence": 0.9, "completeness": "verifiable", "status": "candidate"},
            {"memory_id": related_id, "kind": "fact", "title": "相关标题", "body": "相关正文", "scope": "project", "confidence": 0.7, "completeness": "verifiable", "status": "candidate"},
        ],
        "duplicate_groups": [{"group_id": "dup", "member_ids": [memory_id, related_id]}],
    }, ensure_ascii=False), encoding="utf-8")
    (proj_dir / "neuron.json").write_text(json.dumps({
        "snapshot_id": "s",
        "built_at": "",
        "nodes": [{"id": "claim-" + memory_id[:12], "parent_id": "topic-fact", "label": "旧节点", "node_kind": "claim_anchor", "memory_id": memory_id, "kind": "fact"}],
        "edges": [],
    }, ensure_ascii=False), encoding="utf-8")

    graph = GovernanceApi(str(tmp_path)).get_neuron_graph()
    node = graph["nodes"][0]

    assert node["title"] == "正文标题"
    assert node["body"] == "这是一段真实正文"
    assert node["related"][0]["memory_id"] == related_id


def test_get_neuron_graph_adds_chinese_assistive_fields_for_english_memory(tmp_path) -> None:
    ir_dir = tmp_path / ".memoryguard" / "ir"
    proj_dir = tmp_path / ".memoryguard" / "projections"
    ir_dir.mkdir(parents=True)
    proj_dir.mkdir(parents=True)
    memory_id = "c" * 16
    (ir_dir / "current.json").write_text(json.dumps({
        "records": [{
            "memory_id": memory_id,
            "kind": "preference",
            "title": "Prefer compact project memory rules",
            "body": "The agent should use project memory files as the source of truth and avoid unrelated global preferences.",
            "scope": "project",
            "confidence": 0.8,
            "completeness": "verifiable",
            "status": "candidate",
        }],
        "duplicate_groups": [],
    }, ensure_ascii=False), encoding="utf-8")
    (proj_dir / "neuron.json").write_text(json.dumps({
        "snapshot_id": "s",
        "built_at": "",
        "nodes": [{"id": "claim-" + memory_id[:12], "parent_id": "topic-preference", "label": "old", "node_kind": "claim_anchor", "memory_id": memory_id, "kind": "preference"}],
        "edges": [],
    }, ensure_ascii=False), encoding="utf-8")

    node = GovernanceApi(str(tmp_path)).get_neuron_graph()["nodes"][0]

    assert node["original_title"] == "Prefer compact project memory rules"
    assert node["title_zh"].startswith("偏好：")
    assert node["body_zh"].startswith("中文辅助摘要：")
