"""P1 semantic scope inference tests.

One-sentence creation must read the text for scope signals instead of a fixed
table, but only the trusted current agent / agent+project may ever be selected
automatically.  Broad or ambiguous requests fall back to the narrowest trusted
scope with a lowered confidence and ``fallback_used`` so the cockpit can ask
for human confirmation instead of claiming a wide scope confidently.
"""
import json
import pytest

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.rule_creation import RuleCreationService
from memoryguard.rule_scope import infer_scope_from_text
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


def test_text_scoped_to_current_project_infers_agent_project():
    result = infer_scope_from_text(
        "本项目提交前必须跑测试", agent_instance_id="a", project_ref="/p",
    )
    assert result.selected.target_type == "agent_project"
    assert result.selected.target_id == "a"
    assert result.selected.project_ref == "/p"
    assert result.selected.confidence >= 0.90
    assert not result.fallback_used


def test_broad_text_never_claims_wide_scope_and_falls_back():
    result = infer_scope_from_text(
        "所有 Agent 都必须用中文提交信息", agent_instance_id="a", project_ref="/p",
    )
    # The broad request must NOT become a confident agent_project claim.
    assert result.fallback_used
    assert result.selected.confidence < 0.80
    assert result.selected.target_type in ("agent", "agent_project")
    assert result.selected.target_id == "a"


def test_no_signal_falls_back_to_safe_current_context():
    with_project = infer_scope_from_text(
        "先运行测试", agent_instance_id="a", project_ref="/p",
    )
    assert with_project.selected.target_type == "agent_project"
    assert with_project.selected.target_id == "a"
    assert with_project.selected.confidence <= 0.85

    no_project = infer_scope_from_text(
        "先运行测试", agent_instance_id="a", project_ref="",
    )
    assert no_project.selected.target_type == "agent"
    assert no_project.selected.target_id == "a"


@pytest.mark.parametrize(
    "text, expected_type, expected_fallback",
    [
        ("本项目以后先跑测试", "agent_project", False),
        ("只让当前 Agent 使用中文", "agent", False),
        ("只让当前 agent 使用中文", "agent", False),
        ("所有 Agent 都必须使用中文", "agent_project", True),
        ("所有 agent 都必须使用中文", "agent_project", True),
        ("子 Agent 不允许发布", "agent_project", True),
        ("以后先运行测试", "agent_project", True),
    ],
)
def test_scope_golden_cases(text, expected_type, expected_fallback):
    result = infer_scope_from_text(text, agent_instance_id="a", project_ref="/p")
    assert result.selected.target_type == expected_type
    assert result.selected.target_id == "a"
    assert result.fallback_used is expected_fallback
    assert any(candidate == result.selected for candidate in result.candidates)


def test_automatic_creation_records_semantic_confidence_and_fallback(tmp_path):
    store, context = _setup(tmp_path)
    service = RuleCreationService(tmp_path, "team", store=store)

    project_scoped = service.create_rule_from_text(
        "本项目先跑测试", context,
    )
    assert project_scoped.status == "created"
    assert project_scoped.target_type == "agent_project"
    assert project_scoped.scope_confidence >= 0.90

    broad = service.create_rule_from_text(
        "全局都必须先跑测试", context,
    )
    # Broad text must not be stored as a confident auto scope; it falls back to
    # the narrowest trusted scope with a low confidence and marks fallback.
    assert broad.status == "created"
    assert broad.target_type == "agent_project"
    assert broad.target_id == "a"
    assert broad.scope_confidence < 0.80
    assert "fallback" in (broad.scope_reason or "")


def test_decision_records_inference_details(tmp_path):
    store, context = _setup(tmp_path)
    service = RuleCreationService(tmp_path, "team", store=store)
    decision = service.create_rule_from_text(
        "本项目禁止记录未脱敏内容", context,
    )
    assert decision.decision_id
    # The decision payload carries the reason/confidence so the cockpit can show
    # why this scope was chosen and offer a correction.
    assert decision.scope_confidence > 0
    assert decision.scope_reason
