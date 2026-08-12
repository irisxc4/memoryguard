from __future__ import annotations

from pathlib import Path

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.codegraph_v2 import CodeGraphStore
from memoryguard.codegraph_v2.graphify_adapter import EXPORT_FORMAT, GraphifyExportAdapter
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


class _Manifest:
    def current(self):
        return {"state": "V2_ACTIVE", "generation": 1}


def _context(root: Path):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-bound",
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id="native-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(root),
        share_group_id="group-bound",
    )


def _export() -> dict:
    return {
        "format": EXPORT_FORMAT,
        "complete": True,
        "graphify_version": "phase9-test",
        "source_digest": "digest-v1",
        "files": [
            {"id": "gui", "path": "src/gui.py", "content_hash": "gui-hash", "language": "python", "source_role": "production", "provenance": "production"},
            {"id": "native", "path": "src/native.py", "content_hash": "native-hash", "language": "python", "source_role": "production", "provenance": "production"},
            {"id": "fixture", "path": "tests/fake.py", "content_hash": "fixture-hash", "language": "python", "source_role": "fixture", "provenance": "fixture"},
        ],
        "nodes": [
            {"id": "control", "file": "gui", "name": "添加知识", "kind": "control", "source_location": "L10", "provenance": "production", "semantic_kind": "gui_control", "source_map": {"host_symbol": "PAGE_HTML", "region_id": "r1"}},
            {"id": "handler", "file": "gui", "name": "addBook", "kind": "function", "source_location": "L20", "provenance": "production", "semantic_kind": "handler"},
            {"id": "api", "file": "gui", "name": "knowledge_add", "kind": "api", "source_location": "L30", "provenance": "production", "semantic_kind": "api_method"},
            {"id": "native", "file": "native", "name": "gui_knowledge_command", "kind": "native", "source_location": "L40", "provenance": "production", "semantic_kind": "native_handler"},
            {"id": "fixture-node", "file": "fixture", "name": "fake_handler", "kind": "function", "source_location": "L1", "provenance": "fixture", "semantic_kind": "handler"},
        ],
        "edges": [
            {"source": "control", "target": "handler", "relation": "references", "context": "control_handler", "source_location": "L10", "provenance": "production"},
            {"source": "handler", "target": "api", "relation": "references", "context": "handler_api", "source_location": "L20", "provenance": "production"},
            {"source": "api", "target": "native", "relation": "references", "context": "api_surface", "source_location": "L30", "provenance": "production"},
            {"source": "fixture-node", "target": "native", "relation": "references", "context": "handler_api", "source_location": "L1", "provenance": "fixture"},
        ],
    }


def test_native_codegraph_canonical_operations_and_production_filter(tmp_path: Path) -> None:
    # The native mutation path never initializes a missing CodeGraph implicitly.
    CodeGraphStore(tmp_path)
    context = _context(tmp_path)
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())

    denied = port.dispatch_mcp(
        "memoryguard_codegraph_update",
        {"export": _export(), "confirmed": False},
        context=context,
        generation=1,
    )
    assert denied["ok"] is False
    assert denied["code"] == "confirmation_required"

    updated = port.dispatch_mcp(
        "memoryguard_codegraph_update",
        {"export": _export(), "confirmed": True, "full_snapshot": True},
        context=context,
        generation=1,
    )
    assert updated["ok"] is True
    assert updated["data"]["status"] == "UPDATED"
    assert updated["data"]["counts"]["source_files"] == 3

    add_book = port.dispatch_mcp(
        "memoryguard_codegraph_query",
        {"query": "addBook", "provenance": "production"},
        context=context,
        generation=1,
    )
    assert add_book["ok"] is True
    assert [item["name"] for item in add_book["data"]["symbols"]] == ["addBook"]

    control = port.dispatch_mcp(
        "memoryguard_codegraph_query",
        {"query": "添加知识", "provenance": "production"},
        context=context,
        generation=1,
    )["data"]["symbols"][0]
    native = port.dispatch_mcp(
        "memoryguard_codegraph_query",
        {"query": "gui_knowledge_command", "provenance": "production"},
        context=context,
        generation=1,
    )["data"]["symbols"][0]

    path = port.dispatch_mcp(
        "memoryguard_codegraph_path",
        {"start_id": control["symbol_id"], "end_id": native["symbol_id"], "provenance": "production", "max_depth": 8},
        context=context,
        generation=1,
    )
    assert path["ok"] is True
    assert path["data"]["found"] is True
    assert path["data"]["hops"] == 3

    explain = port.dispatch_mcp(
        "memoryguard_codegraph_explain",
        {"symbol_id": control["symbol_id"], "provenance": "production"},
        context=context,
        generation=1,
    )
    assert explain["ok"] is True
    assert explain["data"]["symbol"]["source_map"]["host_symbol"] == "PAGE_HTML"

    affected = port.dispatch_mcp(
        "memoryguard_codegraph_affected",
        {"start_id": native["symbol_id"], "provenance": "production", "depth": 8},
        context=context,
        generation=1,
    )
    assert affected["ok"] is True
    assert control["symbol_id"] in affected["data"]["result_ids"]
    fixture = port.dispatch_mcp(
        "memoryguard_codegraph_query",
        {"query": "fake_handler", "provenance": "fixture"},
        context=context,
        generation=1,
    )["data"]["symbols"][0]
    assert fixture["symbol_id"] not in affected["data"]["result_ids"]

    graph = port.dispatch_mcp(
        "memoryguard_neuron_graph",
        {"provenance": "production", "limit": 100},
        context=context,
        generation=1,
    )
    assert graph["ok"] is True
    assert all(node.get("provenance") == "production" for node in graph["data"]["nodes"])
    assert all(edge.get("provenance") == "production" for edge in graph["data"]["edges"])

    status = port.dispatch_mcp("memoryguard_codegraph_status", {}, context=context, generation=1)
    assert status["ok"] is True
    assert "production_complete" not in status["data"]
    assert "graphify" in status["data"]
    if status["data"]["update_ready"] is False:
        assert status["data"]["capability_error"]


def test_native_codegraph_update_rejects_raw_graphify_graph(tmp_path: Path) -> None:
    CodeGraphStore(tmp_path)
    result = NativeV2RuntimePort(tmp_path, state_provider=_Manifest()).dispatch_mcp(
        "memoryguard_codegraph_update",
        {"export": {"nodes": [], "links": []}, "confirmed": True},
        context=_context(tmp_path),
        generation=1,
    )
    assert result["ok"] is False
    assert result["code"] == "graphify_export_format_unsupported"


def test_native_codegraph_update_keeps_safe_exception_diagnostic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    CodeGraphStore(tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError("internal detail must stay private")

    monkeypatch.setattr(GraphifyExportAdapter, "project", fail)
    result = NativeV2RuntimePort(tmp_path, state_provider=_Manifest()).dispatch_mcp(
        "memoryguard_codegraph_update",
        {"export": _export(), "confirmed": True},
        context=_context(tmp_path),
        generation=1,
    )
    assert result["ok"] is False
    assert result["code"] == "codegraph_update_failed_runtimeerror"
    assert "internal detail" not in str(result)
