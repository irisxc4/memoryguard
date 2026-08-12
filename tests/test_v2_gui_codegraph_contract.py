from __future__ import annotations

from pathlib import Path

from memoryguard.codegraph_v2 import CodeGraphScope, CodeGraphStore
from memoryguard.codegraph_v2.graphify_adapter import EXPORT_FORMAT, GraphifyExportAdapter
from memoryguard.cutover_v2.gui_contract import codegraph_gui_coverage, codegraph_gui_issues


def _scope(root: Path) -> CodeGraphScope:
    return CodeGraphScope(str(root), "agent", "project", "codex", "group", "gui")


def _write_gui_fixture(root: Path) -> None:
    (root / "interactive.py").write_text("callApi('knowledge_add')\n", encoding="utf-8")
    (root / "knowledge_gui.py").write_text("", encoding="utf-8")
    (root / "gui.py").write_text("", encoding="utf-8")


def _export(*, handler_provenance: str = "production") -> dict:
    return {
        "format": EXPORT_FORMAT,
        "complete": True,
        "graphify_version": "phase9-test",
        "files": [
            {"id": "gui", "path": "interactive.py", "content_hash": "h1", "source_role": "production", "provenance": "production", "language": "python"},
            {"id": "runtime", "path": "runtime.py", "content_hash": "h2", "source_role": "production", "provenance": "production", "language": "python"},
            {"id": "handler-file", "path": "handler.py", "content_hash": "h3", "source_role": handler_provenance, "provenance": handler_provenance, "language": "python"},
        ],
        "nodes": [
            {"id": "control", "file": "gui", "name": "添加知识", "kind": "control", "source_location": "L1", "provenance": "production", "semantic_kind": "gui_control"},
            {"id": "handler", "file": "handler-file", "name": "addBook", "kind": "function", "source_location": "L2", "provenance": handler_provenance, "semantic_kind": "handler"},
            {"id": "api", "file": "gui", "name": "knowledge_add", "kind": "api", "source_location": "L3", "provenance": "production", "semantic_kind": "api_method"},
            {"id": "surface", "file": "runtime", "name": "GuiOperationSpec:knowledge_add", "kind": "surface", "source_location": "L4", "provenance": "production", "semantic_kind": "surface_spec"},
            {"id": "native", "file": "runtime", "name": "gui_knowledge_command", "kind": "native", "source_location": "L5", "provenance": "production", "semantic_kind": "native_handler"},
        ],
        "edges": [
            {"source": "control", "target": "handler", "relation": "references", "context": "control_handler", "source_location": "L1", "provenance": "production"},
            {"source": "handler", "target": "api", "relation": "references", "context": "handler_api", "source_location": "L2", "provenance": "production" if handler_provenance == "production" else handler_provenance},
            {"source": "api", "target": "surface", "relation": "references", "context": "api_surface", "source_location": "L3", "provenance": "production"},
            {"source": "surface", "target": "native", "relation": "references", "context": "surface_native", "source_location": "L4", "provenance": "production"},
        ],
    }


def test_gui_codegraph_validator_requires_complete_production_semantic_chain(tmp_path: Path) -> None:
    _write_gui_fixture(tmp_path)
    store = CodeGraphStore(tmp_path)
    scope = _scope(tmp_path)
    GraphifyExportAdapter(store).project(_export(), scope=scope)

    assert codegraph_gui_issues(store, scope, root=tmp_path) == ()
    coverage = codegraph_gui_coverage(store, scope, root=tmp_path)
    assert coverage["total"] == 1
    assert coverage["mapped"] == 1
    assert coverage["unmapped"] == 0
    assert coverage["complete"] is True


def test_gui_codegraph_validator_rejects_fixture_chain(tmp_path: Path) -> None:
    _write_gui_fixture(tmp_path)
    store = CodeGraphStore(tmp_path)
    scope = _scope(tmp_path)
    GraphifyExportAdapter(store).project(_export(handler_provenance="fixture"), scope=scope)

    issues = codegraph_gui_issues(store, scope, root=tmp_path)
    assert {item["code"] for item in issues} == {
        "codegraph_gui_control_handler_missing",
    }
    assert all(str(item.get("name") or "").startswith("control:") for item in issues)
    coverage = codegraph_gui_coverage(store, scope, root=tmp_path)
    assert coverage["mapped"] == 1
    assert coverage["controls_mapped"] == 0
    assert coverage["complete"] is False
