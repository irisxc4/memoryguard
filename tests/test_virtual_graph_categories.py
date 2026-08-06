"""Virtual graph categories stay indexes, never a second memory layer."""
from __future__ import annotations

from pathlib import Path

from memoryguard.adapters import ImportedConversation
from memoryguard.agent_binding import AgentBindingStore
from memoryguard.conversation_history import ConversationHistoryStore, HistoryScope
from memoryguard.gui import GovernanceApi
from memoryguard.interactive import render_interactive_html
from memoryguard.schema_v3 import MemoryKind, SharedMemoryRecord, SharedMemoryStatus
from memoryguard.shared_memory_store import SharedMemoryStore


def _api_with_agent(workspace: Path) -> GovernanceApi:
    AgentBindingStore(workspace).bind_agent("agent-a", "group-a")
    store = SharedMemoryStore(workspace, "group-a")
    store.append_record(SharedMemoryRecord(
        memory_id="rule-a", body="Never expose raw history", kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE, injection_policy="relevant",
        agent_instance_id="agent-a",
    ))
    ConversationHistoryStore(workspace).import_conversations([
        ImportedConversation(
            conv_id="old-session", title="Old session",
            messages=[{"role": "user", "content": "private old prompt", "created_at": "2026-01-01"}],
        ),
    ], provider="codex", scope=HistoryScope(agent_instance_id="agent-a"))
    return GovernanceApi(workspace)


def test_agent_graph_has_private_virtual_categories_without_raw_history(tmp_path: Path) -> None:
    graph = _api_with_agent(tmp_path).get_neuron_graph(
        scope={"mode": "agent", "agent_instance_id": "agent-a"},
    )

    by_id = {node["id"]: node for node in graph["nodes"]}
    assert by_id["virtual-rules-habits"]["node_kind"] == "virtual_category"
    assert by_id["virtual-rules-habits"]["count"] == 1
    rule_ref = next(node for node in graph["nodes"] if node["node_kind"] == "virtual_rule_ref")
    assert set(rule_ref) >= {
        "memory_id", "body", "kind", "status", "injection_policy",
        "priority", "assignments", "audience", "confidence", "locked",
    }
    assert rule_ref["body"] == "Never expose raw history"
    assert rule_ref["label"] == "Never expose raw history"
    assert by_id["virtual-conversation-history"]["node_kind"] == "virtual_category"
    assert by_id["virtual-conversation-history"]["total"] == 1
    assert by_id["virtual-conversation-history"]["has_more"] is False
    session = next(node for node in graph["nodes"] if node["node_kind"] == "history_session")
    assert set(session) >= {
        "session_id", "title", "provider", "project_ref", "created_at",
        "imported_at", "summary", "turn_count", "evidence_count",
    }
    assert "content" not in session
    assert "body" not in session
    assert "memory_id" not in session
    assert "private old prompt" not in repr(graph)
    assert any(edge["edge_type"] == "virtual_index" for edge in graph["edges"])


def test_empty_projection_still_exposes_virtual_categories(tmp_path: Path) -> None:
    api = GovernanceApi(tmp_path)
    graph = api.get_neuron_graph(scope={"mode": "agent", "agent_instance_id": "unbound"})

    assert graph["empty"] is True
    assert graph["virtual_overlay_available"] is True
    assert {node["id"] for node in graph["nodes"]} >= {
        "main", "virtual-rules-habits", "virtual-conversation-history",
    }
    rules = next(node for node in graph["nodes"] if node["id"] == "virtual-rules-habits")
    assert rules["load_error"] == "rules_require_bound_agent"


def test_share_group_graph_aggregates_safe_project_agent_history(tmp_path: Path) -> None:
    api = _api_with_agent(tmp_path)
    graph = api.get_neuron_graph(scope={"mode": "share_group", "share_group_id": "group-a"})

    history = next(node for node in graph["nodes"] if node["id"] == "virtual-conversation-history")
    assert history["count"] == 1
    assert not history.get("requires_agent_selection")
    kinds = {node["node_kind"] for node in graph["nodes"]}
    assert {"history_project", "history_agent", "history_session"} <= kinds
    history_nodes = [node for node in graph["nodes"] if node.get("virtual_category") == "conversation_history"]
    assert all("content" not in node and "body" not in node and "memory_id" not in node for node in history_nodes)


def test_graph_merges_legacy_project_path_aliases(tmp_path: Path) -> None:
    AgentBindingStore(tmp_path).bind_agent("agent-a", "group-a")
    project = tmp_path / "游戏项目"
    project.mkdir()
    store = ConversationHistoryStore(tmp_path)
    with store._connect() as conn:
        for index, raw_ref in enumerate((str(project), str(project).replace("\\", "/").lower())):
            conn.execute(
                "INSERT INTO conversation_sessions(session_id,external_id,title,provider,agent_instance_id,project_ref,share_group_id,created_at,imported_at,deleted_at) "
                "VALUES (?,?,?,?,?,?,?,?,?, '')",
                (f"legacy-graph-{index}", f"external-{index}", "legacy", "codex", "agent-a", raw_ref, "", "2026-07-30", "2026-07-30"),
            )
    graph = GovernanceApi(tmp_path).get_neuron_graph(
        scope={"mode": "agent", "agent_instance_id": "agent-a"},
    )
    projects = [node for node in graph["nodes"] if node.get("node_kind") == "history_project"]
    assert len(projects) == 1
    assert projects[0]["project_ref"] == HistoryScope(agent_instance_id="agent-a", project_ref=str(project)).project_ref


def test_virtual_graph_and_history_ui_route_without_inline_untrusted_ids() -> None:
    html = render_interactive_html()

    assert "virtual_category" in html
    assert "history_session" in html
    assert "routeVirtualNeuron" in html
    assert "historyFocusSessionId" in html
    assert "discover_local_history_sources" in html
    assert "backfill_local_history" in html
    assert "renderHistoryBackfillPanel" in html
    assert "graph.empty && !graph.virtual_overlay_available" in html
    assert "构建基础投影" not in html
    assert "基础投影尚未构建" not in html
    assert "window.__MG_SESSION__ || (window.pywebview && window.pywebview.api)" in html
    assert "the server-issued session" in html
    assert 'node[record_kind = "rules_habits"]' in html
    assert 'node[record_kind = "conversation_history"]' in html
    assert "source.status === 'importable'" in html
    assert "Array.isArray(data.errors)" in html
    assert "部分导入（会话已索引）" in html
    assert "data-session-id=\"${escapeHtml(s.session_id)}\"" in html
    assert "onclick=\"readHistorySession(" not in html


def test_virtual_nodes_stay_in_graph_and_expose_safe_governance_actions() -> None:
    html = render_interactive_html()

    # Primary Cytoscape taps always select a node; category navigation is only
    # available through an explicit rail action.
    assert "if (node && node.virtual_category) return routeVirtualNeuron(node);" not in html
    assert "cyInstance.on('tap', 'node'" in html
    assert "selectNeuron(event.target.id());" in html
    assert 'data-mg-action="neuron-open-virtual"' in html
    assert 'data-mg-action="neuron-select-node"' in html
    assert 'data-mg-action="neuron-rule-edit-body"' in html
    assert 'data-mg-action="neuron-rule-delete"' in html
    assert 'data-mg-action="neuron-rule-restore"' in html
    assert "ensureRuleAudienceEditor(memoryId)" in html
    assert "callApi('edit_memory', memoryId, body, activeShareGroupId || 'default')" in html
    assert "callApi(method, memoryId, activeShareGroupId || 'default')" in html
    assert 'data-mg-action="neuron-history-read"' in html
    assert "openNeuronHistorySession(sessionId)" in html
    assert "原文不会进入长期记忆或 bootstrap" in html


def test_virtual_categories_have_main_light_edges(tmp_path: Path) -> None:
    graph = _api_with_agent(tmp_path).get_neuron_graph(
        scope={"mode": "agent", "agent_instance_id": "agent-a"},
    )
    edges = {(edge["source"], edge["target"]) for edge in graph["edges"]}

    assert ("main", "virtual-rules-habits") in edges
    assert ("main", "virtual-conversation-history") in edges
