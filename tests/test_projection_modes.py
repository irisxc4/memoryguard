"""Projection mode and exact-scope assertions for the V2 store."""

from __future__ import annotations

import json
from pathlib import Path

from _publish_helpers import build_projection, projection_scope, seed_atom
from memoryguard.projection_v2 import ProjectionStore
from memoryguard.runtime_v2.projection_build import ProjectionBuildService


def test_projection_builder_stores_native_and_reconstructed_separately(tmp_path: Path) -> None:
    seed_atom(tmp_path, "mode-memory", "mode fixture", metadata={"title": "模式", "scope": "project"})
    scope = projection_scope(tmp_path)
    service = ProjectionBuildService(tmp_path)
    store = ProjectionStore(tmp_path, initialize=False)

    assert build_projection(tmp_path, mode="native", scope=scope)["status"] == "succeeded"
    assert build_projection(tmp_path, mode="reconstructed", scope=scope)["status"] == "succeeded"

    native_key = service._scope_key("native", scope)
    reconstructed_key = service._scope_key("reconstructed", scope)
    native = store.get_projection("profile", native_key, scope=scope)
    reconstructed = store.get_projection("scenario", reconstructed_key, scope=scope)
    assert native is not None
    assert reconstructed is not None
    assert native.payload["metadata"]["mode"] == "native"
    assert reconstructed.payload["metadata"]["mode"] == "reconstructed"
    assert native_key != reconstructed_key

    service.delete(mode="native", scope=scope)

    assert store.get_projection("profile", native_key, scope=scope) is None
    assert store.get_projection("scenario", reconstructed_key, scope=scope) is not None


def test_scoped_projection_does_not_fallback_to_legacy_global(tmp_path: Path) -> None:
    legacy_path = tmp_path / ".memoryguard" / "projections" / "reconstructed.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        '{"snapshot_id":"old","built_at":"","nodes":[{"id":"main"}],"edges":[],"stats":{},"meta":{}}',
        encoding="utf-8",
    )
    seed_atom(
        tmp_path,
        "scoped-memory",
        "scoped fixture",
        agent_id="agent-a",
        share_group_id="group-a",
        metadata={"title": "Scoped", "scope": "project"},
    )
    scope_a = projection_scope(tmp_path, agent_id="agent-a", share_group_id="group-a")
    scope_b = projection_scope(tmp_path, agent_id="agent-b", share_group_id="group-b")
    service = ProjectionBuildService(tmp_path)

    assert service.current(mode="reconstructed", scope=scope_b)["projection"] is None
    assert build_projection(tmp_path, scope=scope_a)["status"] == "succeeded"
    assert service.current(mode="reconstructed", scope=scope_b)["projection"] is None
    assert legacy_path.exists()
    assert json.loads(legacy_path.read_text(encoding="utf-8"))["snapshot_id"] == "old"


def test_v2_projection_reads_require_an_exact_scope(tmp_path: Path) -> None:
    seed_atom(tmp_path, "scope-memory", "scope fixture", metadata={"title": "Scope", "scope": "project"})
    scope = projection_scope(tmp_path)
    service = ProjectionBuildService(tmp_path)
    assert build_projection(tmp_path, scope=scope)["status"] == "succeeded"
    key = service._scope_key("reconstructed", scope)
    store = ProjectionStore(tmp_path, initialize=False)

    assert store.get_projection("scenario", key, scope=scope) is not None
    assert store.get_projection("scenario", key, scope=None) is None
