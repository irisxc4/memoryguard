from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.cli import main as cli_main
from memoryguard.codegraph_v2.graphify_adapter import EXPORT_FORMAT
from memoryguard.content.store import ContentStore
from memoryguard.content.conversation_sync import ConversationEvent, ConversationSync
from memoryguard.evidence.store import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.gui import GovernanceApi
from memoryguard.memory.store import MemoryAtom, MemoryAtomStore
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.working_memory import RuntimeStore
from memoryguard.projection_v2.store import ProjectionStore
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.assets_v2.store import AssetStore
from memoryguard.codegraph_v2.store import CodeGraphStore
from memoryguard.skills_v2.store import SkillStore
from memoryguard.system.manifest import ManifestManager, ManifestState
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all


AGENT = "agent-1"
GROUP = "shared-team"
MEMORY_ID = "legacy-memory"
MEMORY_BODY = "migrated memory remains visible through GUI V2"


def _write_v1_fixture(root: Path) -> None:
    database = root / ".memoryguard" / "shared-memory" / GROUP / "memory.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE records ("
            "memory_id TEXT PRIMARY KEY, body TEXT, kind TEXT, status TEXT, "
            "confidence REAL, locked INTEGER, injection_policy TEXT, priority INTEGER, "
            "supersedes TEXT, provenance TEXT, agent_instance_id TEXT, created_at TEXT, "
            "updated_at TEXT, canonical_hash TEXT, dedup_domain TEXT)"
        )
        connection.execute(
            "CREATE TABLE rule_assignments ("
            "memory_id TEXT, target_type TEXT, target_id TEXT, project_ref TEXT, "
            "effect TEXT, priority_override INTEGER, created_at TEXT, updated_at TEXT)"
        )
        connection.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                MEMORY_ID,
                MEMORY_BODY,
                "fact",
                "active",
                0.9,
                0,
                "relevant",
                0,
                "[]",
                "[]",
                AGENT,
                "2026-08-01T00:00:00+00:00",
                "2026-08-01T00:00:00+00:00",
                hashlib.sha256(MEMORY_BODY.encode()).hexdigest(),
                "relevant",
            ),
        )
        connection.commit()

    binding = root / ".memoryguard" / "agent-bindings" / "binding-1.json"
    binding.parent.mkdir(parents=True, exist_ok=True)
    binding.write_text(
        json.dumps(
            {
                "binding_id": "binding-1",
                "agent_instance_id": AGENT,
                "share_group_id": GROUP,
                "mcp_server_name": "memoryguard",
                "native_memory_mode": "observed",
                "status": "active",
                "redirect_paths": [],
                "bound_at": "2026-08-01T00:00:00+00:00",
                "last_drift_check": "",
            }
        ),
        encoding="utf-8",
    )


def _gui_api(root: Path) -> GovernanceApi:
    access = AccessContext(
        trusted_agent_id=AGENT,
        is_admin=True,
        strict_binding=True,
        allow_anon=False,
        session_id="v2-gui-acceptance",
        session_source="transport",
        session_trusted=True,
    )
    return GovernanceApi(
        str(root),
        direct_mutations=True,
        _trusted_access_context=access,
    )


@pytest.fixture
def v1_upgrade_report(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MEMORYGUARD_SANDBOX", "0")
    root = tmp_path / "global-memoryguard-home"
    _write_v1_fixture(root)

    exit_code = cli_main(
        ["upgrade", "--workspace", str(root), "--data-home", str(root)]
    )
    report = json.loads(capsys.readouterr().out)
    return root, exit_code, report


@pytest.fixture
def active_gui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Test-only V2_ACTIVE runtime fixture for post-cutover contracts."""
    monkeypatch.setenv("MEMORYGUARD_SANDBOX", "0")
    root = tmp_path / "active-v2"
    layout = WorkspaceV2Layout(root)
    initialize_all(layout)
    MemoryAtomStore(root)
    EvidenceStore(root)
    RuleV2Store(root)
    ProjectionStore(root)
    ContentStore(root)
    RuntimeStore(root)
    CodeGraphStore(root)
    AssetStore(root)
    SkillStore(root)
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    context = V2MutationContext(
        workspace_id=str(root.resolve()),
        share_group_id=GROUP,
        agent_instance_id=AGENT,
        project_ref=str(root.resolve()),
        provider="gui",
        runtime_role="gui",
        actor="acceptance-fixture",
    )
    item_evidence, _ = governance.put_evidence(
        context=context,
        reason="GUI acceptance fixture",
        source_ref="fixture:legacy-memory",
        digest=hashlib.sha256(MEMORY_BODY.encode()).hexdigest(),
        authority="governance",
    )
    atom, _ = governance.put_atom(
        MemoryAtom(
            memory_id=MEMORY_ID,
            body=MEMORY_BODY,
            workspace_id=str(root.resolve()),
            share_group_id=GROUP,
            agent_instance_id=AGENT,
            project_ref=str(root.resolve()),
            provider="gui",
            runtime_role="gui",
        ),
        context=context,
        evidence=[item_evidence.to_dict()],
        reason="GUI acceptance fixture memory",
        idempotency_key="gui-acceptance-memory",
    )
    memory.project_evidence(evidence)
    memory.set_visibility("active", atom_ids=[atom.atom_id])
    GroupControlService(root, write=True).bind_agent(AGENT, GROUP)
    manager = ManifestManager(root)
    manager.transition(ManifestState.V2_BUILDING, migration_id="gui-acceptance-fixture")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="gui-acceptance-source",
        target_digest="gui-acceptance-target",
        manifest_digest="gui-acceptance-manifest",
        digests={"validator_passed": True, "checkpoints": {"gui": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE
    return root, _gui_api(root)


def _data(result: dict) -> dict:
    assert result.get("ok") is True, result
    value = result.get("data")
    assert isinstance(value, dict), result
    return value


def _wait_for_projection(api: GovernanceApi, run_id: str) -> dict:
    deadline = time.monotonic() + 10
    latest: dict = {}
    while time.monotonic() < deadline:
        latest = api.get_build_progress(run_id)
        if latest.get("status") in {"succeeded", "failed", "cancelled"}:
            return latest
        time.sleep(0.02)
    return latest


def test_v1_upgrade_to_active_keeps_migrated_memory_visible_in_gui(
    v1_upgrade_report,
) -> None:
    root, exit_code, report = v1_upgrade_report
    assert exit_code == 0, report
    assert report["status"] == ManifestState.V2_ACTIVE.value, report
    assert ManifestManager(root).current().state is ManifestState.V2_ACTIVE
    api = _gui_api(root)

    listed = api.list_memory(status="active", share_group_id=GROUP)
    assert listed["ok"] is True, listed
    rows = listed["data"]
    assert any(
        row.get("memory_id") == MEMORY_ID and row.get("body") == MEMORY_BODY
        for row in rows
    ), rows


def test_gui_get_memory_returns_body_for_selected_node(active_gui) -> None:
    _root, api = active_gui
    memory = _data(api.get_memory(MEMORY_ID, GROUP))
    assert memory["memory_id"] == MEMORY_ID
    assert memory["body"] == MEMORY_BODY
    assert memory["share_group_id"] == GROUP


def test_production_neuron_overlay_reads_real_rule_and_history_stores(active_gui) -> None:
    root, api = active_gui
    rules = RuleV2Store(root)
    definition = rules.upsert_definition(build_definition("run full tests before commit", kind="procedure", rule_strength="must"))
    binding = build_binding(
        definition.definition_id, share_group_id=GROUP, target_type="agent",
        target_id=AGENT, owner_agent_id=AGENT, binding_id="e2e-overlay-rule",
    )
    rules.upsert_binding({**binding.to_dict(), "status": "active"})
    rules.upsert_source_link(
        source_kind="native", share_group_id=GROUP, memory_id=MEMORY_ID,
        source_ref=f"rule:{definition.definition_id}",
        original_definition_id=definition.definition_id,
        canonical_definition_id=definition.definition_id, status="active",
    )
    ConversationSync(ContentStore(root)).sync(
        "e2e-history-source",
        [ConversationEvent(
            external_object_key="e2e-session", event_id="e2e-turn",
            content="history body stays outside the neuron graph", role="user", ordinal=0,
            title="Production overlay session", provider="codex",
            workspace_id=str(root.resolve()), agent_instance_id=AGENT,
            project_ref=str(root.resolve()), share_group_id=GROUP,
        )],
        owner_id="e2e-overlay-test",
    )
    result = api.get_neuron_graph()
    data = _data(result)
    by_id = {str(node.get("id") or ""): node for node in data.get("nodes", [])}
    assert data["virtual_overlay_available"] is True
    assert by_id["virtual-rules-habits"]["count"] == 1
    assert by_id["virtual-rules-habits"]["record_kind"] == "rules_habits"
    assert by_id["virtual-conversation-history"]["count"] == 1
    assert by_id["virtual-conversation-history"]["record_kind"] == "conversation_history"
    assert any(node.get("node_kind") == "virtual_rule_ref" for node in data["nodes"])
    assert any(node.get("node_kind") == "history_session" and node.get("title") == "Production overlay session" for node in data["nodes"])
    assert "history body stays outside the neuron graph" not in json.dumps(data, ensure_ascii=False)


def test_gui_history_read_session_uses_one_selector_through_real_bridge(active_gui) -> None:
    root, api = active_gui
    ConversationSync(ContentStore(root)).sync(
        "e2e-history-read-source",
        [ConversationEvent(
            external_object_key="e2e-history-read-session",
            event_id="e2e-history-read-turn",
            content="真实 bridge 必须返回这段会话原文",
            role="user",
            ordinal=0,
            title="真实历史读取",
            provider="gui",
            workspace_id=str(root.resolve()),
            agent_instance_id=AGENT,
            project_ref=str(root.resolve()),
            share_group_id=GROUP,
        )],
        owner_id="e2e-history-read-test",
    )
    with ContentStore(root).connection() as conn:
        session_id = str(conn.execute(
            "SELECT session_id FROM conversation_sessions WHERE title='真实历史读取'"
        ).fetchone()[0])

    result = api.dispatch_api(
        "history_read",
        [{"session_id": session_id, "limit": 100, "offset": 0}],
    )

    assert result["ok"] is True, json.dumps(result, ensure_ascii=False, sort_keys=True)
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    assert data.get("turns"), json.dumps(result, ensure_ascii=False, sort_keys=True)
    assert data["turns"][0]["content"] == "真实 bridge 必须返回这段会话原文"


def test_gui_shared_group_dissolve_hides_active_group_preserves_data_and_rebinds(
    active_gui,
) -> None:
    _root, api = active_gui

    bound = api.bind_agents_to_shared_group(
        [AGENT, "agent-2"], GROUP, "memoryguard", {}, {}, False
    )
    assert bound["ok"] is True, bound
    assert _data(bound)["member_count"] == 2
    assert GROUP in {
        item["share_group_id"] for item in _data(api.list_share_groups())["groups"]
    }

    dissolved = api.dissolve_shared_group(GROUP, True, True)
    dissolved_data = _data(dissolved)
    assert dissolved_data["removed_from_active_groups"] is True
    assert dissolved_data["data_preserved"] is True

    preserved = GroupControlService(_root).group_preview(GROUP)
    assert preserved["member_count"] == 0
    assert preserved["memory_count"] == 1

    active_list = api.list_share_groups()
    active_groups = (
        _data(active_list)["groups"] if active_list.get("ok") is True else []
    )
    assert GROUP not in {item["share_group_id"] for item in active_groups}

    restored = api.bind_agents_to_shared_group(
        [AGENT, "agent-2"], GROUP, "memoryguard", {}, {}, False
    )
    if active_list.get("ok") is not True or restored.get("ok") is not True:
        pytest.fail(
            "GUI dissolved-group lifecycle blocked: "
            f"active_list={active_list.get('code')}; "
            f"rebind={restored.get('code')}"
        )
    restored_data = _data(restored)
    assert restored_data["member_count"] == 2
    assert GROUP in {
        item["share_group_id"] for item in _data(api.list_share_groups())["groups"]
    }
    assert _data(api.get_shared_group_preview(GROUP))["memory_count"] == 1


def test_gui_trusted_scope_separates_codegraph_from_memory_projection(
    active_gui,
) -> None:
    root, api = active_gui
    generation = ManifestManager(root).current().generation

    selected = api.set_governance_scope(
        {"mode": "share_group", "share_group_id": GROUP}
    )
    assert _data(selected)["scope"]["share_group_id"] == GROUP
    trusted_scope = api.get_governance_scope_state()
    assert trusted_scope["ok"] is True, trusted_scope
    assert trusted_scope["data"]["principal_agent_instance_id"] == AGENT
    assert trusted_scope["data"]["active_binding"]["share_group_id"] == GROUP

    forged = api.list_memory(status="active", share_group_id="attacker-group")
    assert forged["ok"] is False
    assert forged["code"] == "context_identity_spoof"

    source_file = root / "codegraph-source.py"
    source_file.write_text("def codegraph_only_symbol():\n    return 1\n", encoding="utf-8")
    codegraph_export = {
        "format": EXPORT_FORMAT,
        "complete": True,
        "graphify_version": "v2-gui-acceptance",
        "files": [
            {
                "id": "codegraph-file",
                "path": source_file.name,
                "content_hash": "codegraph-hash",
                "source_role": "production",
                "provenance": "production",
                "language": "python",
            }
        ],
        "nodes": [
            {
                "id": "codegraph-symbol",
                "file": "codegraph-file",
                "name": "codegraph_only_symbol",
                "kind": "function",
                "source_location": "L1",
                "provenance": "production",
                "semantic_kind": "handler",
            }
        ],
        "edges": [],
    }
    native = NativeV2RuntimePort(
        root,
        state_provider=lambda: {
            "state": ManifestState.V2_ACTIVE.value,
            "generation": generation,
        },
    )
    updated = native.dispatch_mcp(
        "memoryguard_codegraph_update",
        {"export": codegraph_export, "confirmed": True, "full_snapshot": True},
        context=api._trusted_bridge_context(),
        generation=generation,
        state=ManifestState.V2_ACTIVE.value,
    )
    assert updated["ok"] is True, updated

    # CodeGraph and Memory Projection are separate V2 endpoints.  The MCP
    # CodeGraph route is used here as the canonical metadata endpoint; the GUI
    # alias is checked separately so a missing frontend registration is not
    # mistaken for a projection result.
    codegraph = native.dispatch_mcp(
        "memoryguard_codegraph_graph",
        {"provenance": "production", "limit": 100},
        context=api._trusted_bridge_context(),
        generation=generation,
        state=ManifestState.V2_ACTIVE.value,
    )
    assert codegraph["ok"] is True, codegraph
    codegraph_data = _data(codegraph)
    assert any(
        node.get("label") == "codegraph_only_symbol"
        for node in codegraph_data["nodes"]
    ), codegraph_data
    assert MEMORY_BODY not in json.dumps(codegraph_data, ensure_ascii=False)

    gui_codegraph = api.dispatch_api(
        "get_codegraph_graph",
        [{"provenance": "production", "limit": 100}],
    )

    projection_root = root / "projection-source.md"
    projection_root.write_text("projection source metadata", encoding="utf-8")
    ContentStore(root).upsert_source_connector(
        source_id="projection-source",
        provider="agent-native",
        source_type="file",
        external_root_key=str(projection_root.resolve()),
        workspace_id=str(root.resolve()),
        enabled=True,
    )

    started = api.start_build_projection(
        True,
        "reconstructed",
        {"mode": "share_group", "share_group_id": GROUP},
        AGENT,
        GROUP,
        "",
        "",
        "deterministic",
    )
    assert started["ok"] is True, started
    assert started["operation"] == "projection_build"
    run_id = str(started["task"]["run_id"])
    finished = _wait_for_projection(api, run_id)
    assert finished["status"] == "succeeded", finished
    projection_result = finished["result_ref"]
    assert projection_result["atom_count"] == 1
    assert "codegraph_only_symbol" not in json.dumps(
        projection_result, ensure_ascii=False
    )

    projection = api.get_neuron_graph(provenance="production")
    projection_data = _data(projection)
    assert any(
        node.get("memory_id") == MEMORY_ID
        for node in projection_data["nodes"]
    ), projection_data
    assert "codegraph_only_symbol" not in json.dumps(
        projection_data, ensure_ascii=False
    )
    assert MEMORY_BODY not in json.dumps(projection_data, ensure_ascii=False)

    source_map = api.get_projection_source_map(
        {"mode": "share_group", "share_group_id": GROUP},
        AGENT,
        GROUP,
        "reconstructed",
    )
    source_map_data = _data(source_map)
    assert source_map["name"] == "get_projection_source_map"
    source_ids = [item["source_id"] for item in source_map_data["entries"]]
    assert "projection-source" in source_ids
    assert len([item for item in source_ids if item.startswith("v2-memory:")]) == 1
    assert source_map_data["summary"]["governed_memory"] == 1
    assert source_map_data["summary"]["buildable_atom_count"] == 1
    assert "codegraph_only_symbol" not in json.dumps(
        source_map_data, ensure_ascii=False
    )

    if gui_codegraph.get("ok") is not True:
        pytest.fail(
            "GUI CodeGraph endpoint blocked: "
            f"code={gui_codegraph.get('code')}; "
            f"error={gui_codegraph.get('error')}"
        )

    native.shutdown(timeout=5)
