"""V2 deduplication and publication tests for the former distiller surface."""

from __future__ import annotations

import copy
from pathlib import Path

from _publish_helpers import (
    build_projection,
    mutation_context,
    projection_scope,
    publish,
    register_publish_target,
    seed_atom,
)
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory import MemoryAtomStore, MemoryReadScope
from memoryguard.runtime_v2.dedup import V2SemanticDeduplicator
from memoryguard.runtime_v2.organizer import V2MemoryOrganizer


def _organizer(workspace: Path) -> V2MemoryOrganizer:
    memory = MemoryAtomStore(workspace, readonly=False)
    return V2MemoryOrganizer(
        workspace,
        "group-test",
        memory_store=memory,
        governance=GovernanceV2(workspace, memory_store=memory),
    )


def _write_duplicate_pair(workspace: Path) -> tuple[dict, dict]:
    organizer = _organizer(workspace)
    payload = {
        "body": "偏好使用简洁的 commit message，避免冗长摘要。",
        "kind": "preference",
        "agent_instance_id": "agent-test",
        "share_group_id": "group-test",
        "project_ref": str(workspace.resolve()),
        "provider": "test",
        "runtime_role": "test",
        "visibility": "active",
        "metadata": {"source_ref": "src-1"},
    }
    first = organizer.write({**payload, "event_id": "m-a"}, context=mutation_context(workspace))
    second = organizer.write({**payload, "event_id": "m-b"}, context=mutation_context(workspace))
    organizer.store.project_evidence(organizer.governance.evidence)
    return first, second


def test_distill_merges_duplicate_group(tmp_path: Path) -> None:
    first, second = _write_duplicate_pair(tmp_path)
    memory = MemoryAtomStore(tmp_path, readonly=True)
    atoms = memory.list_atoms(
        scope={"workspace_id": str(tmp_path.resolve()), "share_group_id": "group-test"},
        include_building=True,
    )

    assert first["mutation_kind"] == "created"
    assert second["mutation_kind"] == "deduplicated"
    assert second["memory_id"] == first["memory_id"]
    assert len(atoms) == 1
    assert any(action["action"] == "merge_provenance" for action in second["actions"])


def test_distill_does_not_mutate_v2_atoms(tmp_path: Path) -> None:
    seed_atom(
        tmp_path,
        "m-a",
        "偏好使用简洁的 commit message。",
        kind="preference",
        metadata={"title": "偏好简洁提交", "scope": "project"},
    )
    memory = MemoryAtomStore(tmp_path, readonly=True)
    scope = {
        "workspace_id": str(tmp_path.resolve()),
        "share_group_id": "group-test",
        "agent_instance_id": "agent-test",
        "project_ref": str(tmp_path.resolve()),
        "provider": "test",
        "runtime_role": "test",
    }
    before = [copy.deepcopy(atom.to_dict()) for atom in memory.list_atoms(scope=scope, include_building=True)]
    matches = V2SemanticDeduplicator(
        memory, MemoryReadScope.from_value(scope), threshold=0.85
    ).find(
        "偏好使用简洁的 commit message。"
    )
    after = [atom.to_dict() for atom in memory.list_atoms(scope=scope, include_building=True)]

    assert matches
    assert after == before


def test_publish_uses_v2_deduplicated_memory_by_default(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "native" / "memory.md"
    seed_atom(
        workspace,
        "m-publish",
        "偏好使用简洁的 commit message。",
        kind="preference",
        metadata={"title": "偏好简洁提交", "scope": "project"},
    )
    scope = projection_scope(workspace)
    register_publish_target(workspace, target)

    built = build_projection(workspace, scope=scope)
    published = publish(workspace, target, scope=scope)

    assert built["status"] == "succeeded"
    assert built["atom_count"] == 1
    assert published["ok"] is True
    assert target.exists()
    assert target.read_text(encoding="utf-8")
    assert not (workspace / ".memoryguard" / "ir" / "distilled.json").exists()


def test_publish_does_not_fallback_to_legacy_raw_memory(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "native" / "memory.md"
    seed_atom(
        workspace,
        "m-raw",
        "当前有效事实",
        metadata={"title": "当前有效事实", "scope": "project"},
    )
    scope = projection_scope(workspace)
    register_publish_target(workspace, target)
    build_projection(workspace, scope=scope)
    monkeypatch.setenv("MEMORYGUARD_PUBLISH_RAW", "1")

    published = publish(workspace, target, scope=scope)

    assert published["ok"] is True
    assert not (workspace / ".memoryguard" / "ir" / "current.json").exists()


def test_v2_projection_skips_superseded_atoms(tmp_path: Path) -> None:
    seed_atom(
        tmp_path,
        "m-active",
        "这条应该被发布。",
        metadata={"title": "当前有效事实", "scope": "project"},
    )
    seed_atom(
        tmp_path,
        "m-old",
        "这条 superseded 正文不应出现在投影。",
        status="superseded",
        metadata={"title": "已被取代的旧事实", "scope": "project"},
    )
    scope = projection_scope(tmp_path)
    result = build_projection(tmp_path, scope=scope)

    assert result["status"] == "succeeded"
    assert result["atom_count"] == 1
    assert result["projection"]["evidence_count"] >= 1
