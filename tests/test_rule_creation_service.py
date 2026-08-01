import json

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.rule_creation import RuleCreationService
from memoryguard.schema_v3 import EffectiveAgentContext
from memoryguard.shared_memory_store import SharedMemoryStore


def _setup(tmp_path, agent="a"):
    AgentBindingStore(tmp_path).bind_agent(agent, "team")
    store = SharedMemoryStore(tmp_path, "team")
    context = EffectiveAgentContext(
        agent_instance_id=agent,
        share_group_id="team",
        project_ref=str(tmp_path / "project"),
    )
    return store, context


def test_auto_create_infers_trusted_agent_project_and_exposes_undo(tmp_path):
    store, context = _setup(tmp_path)
    service = RuleCreationService(tmp_path, "team", store=store)

    result = service.create_rule_from_text("必须先运行测试", context)

    assert result.status == "created"
    assert result.scope_confidence > 0
    assert result.target_type == "agent_project"
    assert result.target_id == "a"
    assert result.project_ref.endswith("/project")
    assert result.undo_id
    assert store.get_record(result.memory_id) is not None
    assert store.list_rule_assignments(result.memory_id)[0].target_type == "agent_project"

    undone = service.undo_rule(result.undo_id, context)
    assert undone.status == "undone"
    assert store.get_record(result.memory_id) is None


def test_auto_scope_rejects_broad_or_other_agent_target(tmp_path):
    store, context = _setup(tmp_path)
    service = RuleCreationService(tmp_path, "team", store=store)

    broad = service.create_rule_from_text(
        "全局必须遵守", context,
        requested_scope={"target_type": "group", "target_id": "team"},
    )
    other = service.create_rule_from_text(
        "他人规则", context,
        requested_scope={"target_type": "agent", "target_id": "b"},
    )

    assert broad.status == "blocked"
    assert "only agent or agent_project" in broad.blocked_reason
    assert other.status == "blocked"
    assert "trusted current agent" in other.blocked_reason
    assert store.list_records() == []


def test_manual_broad_scope_requires_admin_and_is_audited(tmp_path):
    store, context = _setup(tmp_path)
    service = RuleCreationService(tmp_path, "team", store=store)

    denied = service.create_rule_from_text(
        "组规则", context,
        requested_scope={"target_type": "group", "target_id": "team"},
        manual=True,
    )
    assert denied.status == "blocked"

    accepted = service.create_rule_from_text(
        "组规则", context,
        requested_scope={"target_type": "group", "target_id": "team"},
        manual=True,
        is_admin=True,
    )
    assert accepted.status == "created"
    assert store.list_rule_assignments(accepted.memory_id)[0].target_type == "group"
    assert service.read_decision(accepted.decision_id).scope_reason == "explicit human-admin scope declaration"
