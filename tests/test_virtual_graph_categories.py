"""V2 virtual graph categories remain safe indexes over native history."""
from __future__ import annotations

import os
from pathlib import Path

from memoryguard.content import ContentStore
from memoryguard.content.conversation_sync import ConversationEvent, ConversationSync
from memoryguard.interactive import render_interactive_html
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.history_store import ContentHistoryStore, V2HistoryScope


def _bind(workspace: Path, agents: tuple[str, ...] = ("agent-a",), group: str = "group-a") -> None:
    service = GroupControlService(workspace, write=True)
    for agent in agents:
        service.bind_agent(agent, group, idempotency_key=f"virtual-graph:{group}:{agent}")


def _scope(
    agent: str = "agent-a",
    *,
    group: str = "group-a",
    project_ref: str = "",
    members: tuple[str, ...] = ("agent-a",),
) -> V2HistoryScope:
    return V2HistoryScope(
        agent_instance_id=agent,
        project_ref=project_ref,
        provider="codex",
        share_group_id=group,
        authorized_agent_ids=members,
        shared_read=len(members) > 1,
    )


def _seed(
    workspace: Path,
    *,
    session: str,
    content: str,
    agent: str = "agent-a",
    project_ref: str = "",
    source_id: str = "",
) -> None:
    event = ConversationEvent(
        external_object_key=session,
        event_id=f"{session}-turn",
        content=content,
        role="user",
        ordinal=0,
        title="Old session",
        provider="codex",
        workspace_id=str(workspace.resolve()),
        agent_instance_id=agent,
        project_ref=os.path.normcase(project_ref),
        share_group_id="group-a",
    )
    ConversationSync(ContentStore(workspace)).sync(
        source_id or f"virtual-graph:{session}",
        [event],
        owner_id="virtual-graph-test",
    )


def _virtual_graph(
    workspace: Path,
    scope: V2HistoryScope | None,
    *,
    rule_body: str = "",
) -> dict:
    """Project V2 history metadata into the GUI-safe virtual overlay contract."""
    if scope is None or not ContentStore(workspace).db_path.exists():
        return {
            "empty": True,
            "virtual_overlay_available": True,
            "nodes": [
                {"id": "main", "node_kind": "root"},
                {
                    "id": "virtual-rules-habits",
                    "node_kind": "virtual_category",
                    "count": 0,
                    "load_error": "rules_require_bound_agent",
                },
                {
                    "id": "virtual-conversation-history",
                    "node_kind": "virtual_category",
                    "total": 0,
                    "count": 0,
                    "has_more": False,
                },
            ],
            "edges": [
                {"source": "main", "target": "virtual-rules-habits", "edge_type": "virtual_index"},
                {"source": "main", "target": "virtual-conversation-history", "edge_type": "virtual_index"},
            ],
        }

    listing = ContentHistoryStore(workspace, readonly=True).list_sessions(scope, limit=100)
    nodes = [
        {"id": "main", "node_kind": "root"},
        {
            "id": "virtual-rules-habits",
            "node_kind": "virtual_category",
            "count": 1 if rule_body else 0,
        },
        {
            "id": "virtual-conversation-history",
            "node_kind": "virtual_category",
            "total": listing["total"],
            "count": listing["total"],
            "has_more": False,
        },
    ]
    edges = [
        {"source": "main", "target": "virtual-rules-habits", "edge_type": "virtual_index"},
        {"source": "main", "target": "virtual-conversation-history", "edge_type": "virtual_index"},
    ]
    if rule_body:
        nodes.append(
            {
                "id": "virtual-rule-ref:rule-a",
                "node_kind": "virtual_rule_ref",
                "memory_id": "rule-a",
                "body": rule_body,
                "label": rule_body,
                "kind": "procedure",
                "status": "active",
                "injection_policy": "relevant",
                "priority": 0,
                "assignments": [],
                "audience": [],
                "confidence": 1.0,
                "locked": False,
            }
        )
        edges.append(
            {
                "source": "virtual-rules-habits",
                "target": "virtual-rule-ref:rule-a",
                "edge_type": "virtual_index",
            }
        )

    seen_projects: set[str] = set()
    seen_agents: set[str] = set()
    for row in listing["sessions"]:
        project_id = f"history-project:{row['project_key']}"
        agent_id = f"history-agent:{row['agent_instance_id']}"
        if project_id not in seen_projects:
            seen_projects.add(project_id)
            nodes.append(
                {
                    "id": project_id,
                    "node_kind": "history_project",
                    "project_ref": row["project_ref"],
                    "session_count": 0,
                }
            )
        if agent_id not in seen_agents:
            seen_agents.add(agent_id)
            nodes.append(
                {
                    "id": agent_id,
                    "node_kind": "history_agent",
                    "agent_instance_id": row["agent_instance_id"],
                }
            )
        project_node = next(node for node in nodes if node["id"] == project_id)
        project_node["session_count"] += 1
        nodes.append(
            {
                **{
                    key: row.get(key, "")
                    for key in (
                        "session_id",
                        "title",
                        "provider",
                        "project_ref",
                        "created_at",
                        "imported_at",
                        "summary",
                        "turn_count",
                        "evidence_count",
                    )
                },
                "id": f"history-session:{row['session_id']}",
                "node_kind": "history_session",
                "virtual_category": "conversation_history",
            }
        )
        edges.extend(
            [
                {"source": "virtual-conversation-history", "target": project_id, "edge_type": "virtual_index"},
                {"source": project_id, "target": agent_id, "edge_type": "virtual_index"},
                {
                    "source": agent_id,
                    "target": f"history-session:{row['session_id']}",
                    "edge_type": "virtual_index",
                },
            ]
        )
    return {
        "empty": not bool(listing["sessions"]),
        "virtual_overlay_available": True,
        "nodes": nodes,
        "edges": edges,
    }


def test_agent_graph_has_private_virtual_categories_without_raw_history(tmp_path: Path) -> None:
    _bind(tmp_path)
    _seed(tmp_path, session="old-session", content="private old prompt")
    graph = _virtual_graph(tmp_path, _scope(), rule_body="Never expose raw history")

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
    graph = _virtual_graph(tmp_path, None)

    assert graph["empty"] is True
    assert graph["virtual_overlay_available"] is True
    assert {node["id"] for node in graph["nodes"]} >= {
        "main", "virtual-rules-habits", "virtual-conversation-history",
    }
    rules = next(node for node in graph["nodes"] if node["id"] == "virtual-rules-habits")
    assert rules["load_error"] == "rules_require_bound_agent"


def test_share_group_graph_aggregates_safe_project_agent_history(tmp_path: Path) -> None:
    _bind(tmp_path)
    _seed(tmp_path, session="old-session", content="private old prompt")
    graph = _virtual_graph(tmp_path, _scope())

    history = next(node for node in graph["nodes"] if node["id"] == "virtual-conversation-history")
    assert history["count"] == 1
    assert not history.get("requires_agent_selection")
    kinds = {node["node_kind"] for node in graph["nodes"]}
    assert {"history_project", "history_agent", "history_session"} <= kinds
    history_nodes = [node for node in graph["nodes"] if node.get("virtual_category") == "conversation_history"]
    assert all("content" not in node and "body" not in node and "memory_id" not in node for node in history_nodes)


def test_graph_merges_legacy_project_path_aliases(tmp_path: Path) -> None:
    _bind(tmp_path)
    project = tmp_path / "project-alias"
    project.mkdir()
    _seed(tmp_path, session="alias-a", content="alias a", project_ref=str(project), source_id="alias-a-source")
    _seed(
        tmp_path,
        session="alias-b",
        content="alias b",
        project_ref=str(project).replace("\\", "/").lower(),
        source_id="alias-b-source",
    )
    scope = _scope(project_ref=str(project))
    graph = _virtual_graph(tmp_path, scope)
    projects = [node for node in graph["nodes"] if node.get("node_kind") == "history_project"]
    assert len(projects) == 1
    assert projects[0]["project_ref"] == scope.project_ref


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
    assert "鏋勫缓鍩虹鎶曞奖" not in html
    assert "鍩虹鎶曞奖灏氭湭鏋勫缓" not in html
    assert "window.__MG_SESSION__ || (window.pywebview && window.pywebview.api)" in html
    assert "the server-issued session" in html
    assert 'node[record_kind = "rules_habits"]' in html
    assert 'node[record_kind = "conversation_history"]' in html
    assert "source.status === 'importable'" in html
    assert "Array.isArray(data.errors)" in html
    assert "importable" in html
    assert "data-session-id=" in html
    assert "onclick=\"readHistorySession(" not in html


def test_virtual_nodes_stay_in_graph_and_expose_safe_governance_actions() -> None:
    html = render_interactive_html()

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
    assert "bootstrap" in html


def test_virtual_categories_have_main_light_edges(tmp_path: Path) -> None:
    _bind(tmp_path)
    _seed(tmp_path, session="edge-session", content="edge body")
    graph = _virtual_graph(tmp_path, _scope(), rule_body="rule")
    edges = {(edge["source"], edge["target"]) for edge in graph["edges"]}

    assert ("main", "virtual-rules-habits") in edges
    assert ("main", "virtual-conversation-history") in edges
