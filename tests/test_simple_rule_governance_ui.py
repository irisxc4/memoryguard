"""Regression contract for simple rule governance and canonical neuron detail."""

from __future__ import annotations

from pathlib import Path

from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort


ROOT = Path(__file__).resolve().parents[1]
INTERACTIVE = ROOT / "src" / "memoryguard" / "interactive.py"
NATIVE_PORTS = ROOT / "src" / "memoryguard" / "runtime_v2" / "native_ports.py"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_simple_rule_editor_has_human_defaults_and_advanced_foldout() -> None:
    source = _source(INTERACTIVE)

    assert "记忆方式" in source
    assert "普通习惯" in source
    assert "必须遵守" in source
    assert "谁适用" in source
    assert "当前助手" in source
    assert "所有已连接助手" in source
    assert "哪里适用" in source
    assert "所有项目" in source
    assert "当前项目" in source
    assert '<details class="rule-advanced"' in source
    # Internal concepts stay available, but only inside advanced controls.
    assert "target_type" in source
    assert "runtime_role" in source
    assert "原始 ID" in source


def test_agent_identity_contract_has_safe_fallback_and_current_marker() -> None:
    source = _source(INTERACTIVE)

    assert "user_alias" in source
    assert "display_name" in source
    assert "未知助手" in source
    assert "slice(-4)" in source
    assert "（当前）" in source


def test_save_rule_audience_keeps_stable_ids_on_wire() -> None:
    source = _source(INTERACTIVE)

    # Human labels must never become mutation authority.  Existing save path
    # still sends target_id/project_ref values and does not send label fields.
    assert "target_id: targetId" in source
    assert "project_ref: projectRef" in source
    assert "update_rule_audience" in source
    assert "label: targetId" not in source


def test_neuron_contract_marks_canonical_rules_and_detail_branches() -> None:
    source = _source(NATIVE_PORTS)
    html = _source(INTERACTIVE)

    assert '"canonical": True' in source
    assert '"canonical_node_kind": "canonical_rule"' in source
    assert '"detail_branches"' in source
    assert '"branch_type": "source"' in source
    assert '"branch_type": "scope"' in source
    assert '"branch_type": "classification"' in source
    assert "detail_branches" in html
    assert "展开" in html


def test_scope_options_emit_friendly_agent_records_without_guessing() -> None:
    class _AgentService:
        def list_agents(self):
            return {
                "agents": [
                    {
                        "instance_id": "agent-stable-1234",
                        "product": "Codex",
                        "display_name": "Codex CLI",
                        "last_activity_at": "2026-08-15T12:00:00Z",
                    },
                ],
            }

        def discover_agents(self):
            return {"instances": []}

    port = object.__new__(NativeV2RuntimePort)
    port._agent_service = lambda: _AgentService()
    port._trusted_admin = lambda _context: False
    port._group_service = lambda **_kwargs: None
    context = {
        "agent_instance_id": "agent-stable-1234",
        "share_group_id": "group-stable",
        "project_ref": "C:/work/project",
        "provider": "codex",
        "runtime_role": "assistant",
    }

    result = port._gui_rule_scope_options({}, context)
    item = result["agents"][0]
    assert item["id"] == "agent-stable-1234"
    assert item["label"].startswith("Codex CLI")
    assert item["current"] is True
    assert item["product"] == "Codex"
    assert item["last_seen"] == "2026-08-15T12:00:00Z"
    assert "agent-stable-1234" not in item["label"]


def test_scope_options_mark_missing_current_project_fail_closed() -> None:
    class _AgentService:
        def list_agents(self):
            return {"agents": [{"instance_id": "agent-stable-1234", "product": "Codex"}]}

        def discover_agents(self):
            return {"instances": []}

    port = object.__new__(NativeV2RuntimePort)
    port._agent_service = lambda: _AgentService()
    port._trusted_admin = lambda _context: False
    port._group_service = lambda **_kwargs: None

    result = port._gui_rule_scope_options(
        {},
        {
            "agent_instance_id": "agent-stable-1234",
            "share_group_id": "group-stable",
            "project_ref": "",
        },
    )

    assert result["current_project_ref"] == ""
    assert result["current_project_available"] is False


def test_simple_editor_rejects_unconfirmed_current_project_before_mutation() -> None:
    source = _source(INTERACTIVE)

    assert "currentProjectKnown" in source
    assert "当前项目（未确认）" in source
    assert "未确认当前项目，不能保存当前项目范围" in source
    guard = source.index("if (!simpleAssignments)")
    mutation = source.index("callApi('update_rule_audience'")
    assert guard < mutation


def test_trusted_project_keeps_agent_project_wire_shape() -> None:
    source = _source(INTERACTIVE)

    assert "const useCurrentProject = where === 'current_project';" in source
    assert "target_type: useCurrentProject ? 'agent_project' : 'agent'" in source
    assert "project_ref: useCurrentProject ? currentProject : ''" in source
