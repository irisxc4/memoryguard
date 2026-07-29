from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.governance_scope import (
    GovernanceScope,
    authorized_roots_digest,
    grant_root_to_agent,
    scope_storage_key,
)
from memoryguard.gui import GovernanceApi
from memoryguard.projection import ProjectionBuilder
from memoryguard.schema_v3 import SourceRootType
from memoryguard.source_registry import SourceRegistry


def _scoped_proj(tmp_path: Path, agent_id: str, nodes: list, root_ids: list[str]) -> None:
    scope = GovernanceScope(mode="agent", agent_instance_id=agent_id)
    key = scope_storage_key(scope)
    pb = ProjectionBuilder(tmp_path, "reconstructed", scope_key=key)
    pb.proj_path.parent.mkdir(parents=True, exist_ok=True)
    pb.proj_path.write_text(json.dumps({
        "snapshot_id": "s",
        "built_at": "",
        "nodes": nodes,
        "edges": [],
        "meta": {
            "authorized_root_ids": root_ids,
            "authorized_roots_digest": authorized_roots_digest(root_ids),
        },
    }, ensure_ascii=False), encoding="utf-8")


def test_get_neuron_graph_hydrates_legacy_projection_nodes_from_ir(tmp_path) -> None:
    ir_dir = tmp_path / ".memoryguard" / "ir"
    ir_dir.mkdir(parents=True)
    memory_id = "a" * 16
    related_id = "b" * 16
    (ir_dir / "current.json").write_text(json.dumps({
        "records": [
            {"memory_id": memory_id, "kind": "fact", "title": "正文标题", "body": "这是一段真实正文", "scope": "project", "confidence": 0.9, "completeness": "verifiable", "status": "candidate"},
            {"memory_id": related_id, "kind": "fact", "title": "相关标题", "body": "相关正文", "scope": "project", "confidence": 0.7, "completeness": "verifiable", "status": "candidate"},
        ],
        "duplicate_groups": [{"group_id": "dup", "member_ids": [memory_id, related_id]}],
    }, ensure_ascii=False), encoding="utf-8")

    src = tmp_path / "src.md"
    src.write_text("x", encoding="utf-8")
    reg = SourceRegistry(tmp_path)
    root = reg.add(str(src), SourceRootType.SELECTED_FILE, "src")
    root.enabled = True
    grant_root_to_agent(root, "agent-hydrate")
    reg._save()

    _scoped_proj(tmp_path, "agent-hydrate", [
        {"id": "claim-" + memory_id[:12], "parent_id": "topic-fact", "label": "旧节点",
         "node_kind": "claim_anchor", "memory_id": memory_id, "kind": "fact"},
    ], [root.root_id])

    graph = GovernanceApi(str(tmp_path)).get_neuron_graph(agent_instance_id="agent-hydrate")
    node = next(n for n in graph["nodes"] if n.get("memory_id") == memory_id)

    assert node["title"] == "正文标题"
    assert node["body"] == "这是一段真实正文"
    assert node["related"][0]["memory_id"] == related_id


def test_get_neuron_graph_adds_chinese_assistive_fields_for_english_memory(tmp_path) -> None:
    ir_dir = tmp_path / ".memoryguard" / "ir"
    ir_dir.mkdir(parents=True)
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

    src = tmp_path / "src.md"
    src.write_text("x", encoding="utf-8")
    reg = SourceRegistry(tmp_path)
    root = reg.add(str(src), SourceRootType.SELECTED_FILE, "src")
    root.enabled = True
    grant_root_to_agent(root, "agent-zh")
    reg._save()

    _scoped_proj(tmp_path, "agent-zh", [
        {"id": "claim-" + memory_id[:12], "parent_id": "topic-preference", "label": "old",
         "node_kind": "claim_anchor", "memory_id": memory_id, "kind": "preference"},
    ], [root.root_id])

    node = next(
        n for n in GovernanceApi(str(tmp_path)).get_neuron_graph(agent_instance_id="agent-zh")["nodes"]
        if n.get("memory_id") == memory_id
    )

    assert node["original_title"] == "Prefer compact project memory rules"
    assert node["title_zh"].startswith("偏好：")
    assert node["localization_mode"] == "heuristic"
    assert not node["body_zh"].startswith("中文辅助摘要：")
    assert not node["body_zh"].startswith("中文整理：")
