"""Rule cockpit bridge lifecycle tests (real service + binding fallback chain).

These tests exercise the GUI boundary against the real ``RuleCreationService``
and the real ``AgentBindingStore``.  The old fake-service tests only proved the
bridge "calls something"; the real service enforces the decision-ID undo
contract, immutable receipt context and atomic exception revocation that the
GUI must speak.  Agent/group resolution is tested as its own fallback chain:
the group is always derived from the Agent's active binding, never defaulted.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.agent_binding import AgentBindingStore  # noqa: E402
from memoryguard.governance_scope import GovernanceScope  # noqa: E402
from memoryguard.rule_scope import canonical_project_ref  # noqa: E402
from memoryguard.schema_v3 import (  # noqa: E402
    EffectiveAgentContext, RuleMatchReceipt, _now_iso,
)


def _make_api(workspace):
    from memoryguard.gui import GovernanceApi

    api = GovernanceApi(workspace)
    project = canonical_project_ref(str(Path(workspace) / "project"))
    api._rule_scope_options = lambda _group: {
        "agents": [{"id": "agent-a", "label": "agent-a"}, {"id": "agent-b", "label": "agent-b"}],
        "groups": [{"id": _group, "label": _group}],
        "projects": [{"id": project, "label": "project"}],
        "providers": [], "runtime_roles": [],
    }
    return api


def _personal_group(store: AgentBindingStore, agent: str) -> str:
    return str(store.ensure_personal_memory_group(agent)["share_group_id"])


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.delenv("MEMORYGUARD_AGENT_ID", raising=False)
    monkeypatch.delenv("MEMORYGUARD_SHARE_GROUP_ID", raising=False)
    monkeypatch.delenv("MEMORYGUARD_PROJECT_CWD", raising=False)
    return monkeypatch


def test_rule_agent_resolution_env_agent_uses_personal_binding(tmp_path, env):
    store = AgentBindingStore(tmp_path)
    personal = _personal_group(store, "agent-a")
    env.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    api = _make_api(tmp_path)
    agent, group, error = api._resolve_current_rule_agent(None)
    assert error is None
    assert agent == "agent-a"
    assert group == personal


def test_rule_agent_resolution_agent_scope_uses_personal_binding(tmp_path, env):
    store = AgentBindingStore(tmp_path)
    personal = _personal_group(store, "agent-a")
    env.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    api = _make_api(tmp_path)
    preference = GovernanceScope(mode="agent", agent_instance_id="agent-a")
    agent, group, error = api._resolve_current_rule_agent(preference)
    assert error is None
    assert group == personal


def test_rule_agent_resolution_unique_binding_no_preference(tmp_path, env):
    store = AgentBindingStore(tmp_path)
    personal = _personal_group(store, "agent-a")
    env.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    api = _make_api(tmp_path)
    agent, group, error = api._resolve_current_rule_agent(None)
    assert error is None
    assert agent == "agent-a"
    assert group == personal


def test_rule_agent_resolution_env_group_binding_mismatch_rejected(tmp_path, env):
    store = AgentBindingStore(tmp_path)
    _personal_group(store, "agent-a")  # bound to personal-<hash>, not "g1"
    env.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    env.setenv("MEMORYGUARD_SHARE_GROUP_ID", "g1")
    api = _make_api(tmp_path)
    agent, group, error = api._resolve_current_rule_agent(None)
    assert error is not None
    assert error["error"] == "agent_not_bound_to_group"
    assert agent is None
    assert group == "g1"


def test_rule_agent_resolution_ambiguous_group_members_rejected(tmp_path, env):
    store = AgentBindingStore(tmp_path)
    store.bind_agent("agent-a", "g1")
    store.bind_agent("agent-b", "g1")
    env.setenv("MEMORYGUARD_SHARE_GROUP_ID", "g1")
    env.delenv("MEMORYGUARD_AGENT_ID", raising=False)
    api = _make_api(tmp_path)
    agent, group, error = api._resolve_current_rule_agent(None)
    assert error is not None
    assert error["error"] == "ambiguous_agent_context"
    assert agent is None


def test_rule_agent_resolution_first_run_creates_personal_group(tmp_path, env):
    env.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    api = _make_api(tmp_path)
    agent, group, error = api._resolve_current_rule_agent(None)
    assert error is None
    assert agent == "agent-a"
    assert group.startswith("personal-")
    assert AgentBindingStore(tmp_path).find_by_agent("agent-a", include_inactive=False)


def test_gui_real_service_create_then_undo(tmp_path, env):
    from memoryguard.gui import GovernanceApi

    store = AgentBindingStore(tmp_path)
    _personal_group(store, "agent-a")
    env.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    env.setenv("MEMORYGUARD_PROJECT_CWD", str(tmp_path / "project"))
    api = _make_api(tmp_path)
    decision = api.create_rule_from_text("所有任务默认使用 UTF-8。", confirmed=True)
    assert decision["ok"] is True
    assert decision["decision_id"]

    undone = api.undo_rule_decision(decision["decision_id"], confirmed=True)
    assert undone["status"] == "undone"


def test_gui_real_service_exception_revoke_uses_trusted_context(tmp_path, env):
    from memoryguard.gui import GovernanceApi
    from memoryguard.rule_creation import RuleCreationService
    from memoryguard.shared_memory_store import SharedMemoryStore

    group = "g1"
    store = SharedMemoryStore(tmp_path, group)
    AgentBindingStore(tmp_path).bind_agent("agent-a", group)
    env.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    env.setenv("MEMORYGUARD_PROJECT_CWD", str(tmp_path / "project"))
    env.setenv("MEMORYGUARD_SHARE_GROUP_ID", group)
    store.append_record(
        _record(store, "parent"), assignments=[{"target_type": "agent", "target_id": "agent-a"}],
    )
    service = RuleCreationService(tmp_path, group, store=store)
    context = EffectiveAgentContext(
        agent_instance_id="agent-a", share_group_id=group,
        project_ref=str(tmp_path / "project"), session_id="s1",
    )
    store.append_rule_match_receipt(RuleMatchReceipt(
        receipt_id="receipt-1", memory_id="parent", share_group_id=group,
        agent_instance_id="agent-a", task_hash="t", task="task",
        project_ref=str(tmp_path / "project"), session_id="s1",
        created_at=_now_iso(),
    ))
    created = service.submit_feedback(
        "receipt-1", "exception", "agent-a",
        evidence="override body", effective_context=context,
    )
    assert created.status == "created"
    exception_id = created.metadata["exception_id"]

    api = _make_api(tmp_path)
    revoked = api.revoke_rule_exception(exception_id, confirmed=True)
    assert revoked["ok"] is True
    assert revoked["active"] is False


def test_feedback_project_mismatch_is_blocked(tmp_path):
    from memoryguard.rule_creation import RuleCreationService
    from memoryguard.shared_memory_store import SharedMemoryStore

    group = "g1"
    store = SharedMemoryStore(tmp_path, group)
    store.append_record(
        _record(store, "parent"), assignments=[{"target_type": "agent", "target_id": "agent-a"}],
    )
    store.append_rule_match_receipt(RuleMatchReceipt(
        receipt_id="receipt-pa", memory_id="parent", share_group_id=group,
        agent_instance_id="agent-a", task_hash="t", task="task",
        project_ref=str(tmp_path / "projectA"), session_id="s1",
        created_at=_now_iso(),
    ))
    service = RuleCreationService(tmp_path, group, store=store)
    # Submitter switches to project B: evidence from receipt-A must not be
    # allowed to mutate Project B's rule.
    context = EffectiveAgentContext(
        agent_instance_id="agent-a", share_group_id=group,
        project_ref=str(tmp_path / "projectB"), session_id="s1",
    )
    result = service.submit_feedback(
        "receipt-pa", "not_applicable", "agent-a",
        evidence="not applicable", effective_context=context,
    )
    assert result.status == "blocked"
    assert result.blocked_reason == "feedback_context_project_mismatch"


def test_feedback_empty_exception_body_rejected_before_write(tmp_path):
    from memoryguard.rule_creation import RuleCreationService
    from memoryguard.shared_memory_store import SharedMemoryStore

    group = "g1"
    store = SharedMemoryStore(tmp_path, group)
    store.append_record(
        _record(store, "parent"), assignments=[{"target_type": "agent", "target_id": "agent-a"}],
    )
    store.append_rule_match_receipt(RuleMatchReceipt(
        receipt_id="receipt-1", memory_id="parent", share_group_id=group,
        agent_instance_id="agent-a", task_hash="t", task="task",
        project_ref=str(tmp_path / "projectA"), session_id="s1",
        created_at=_now_iso(),
    ))
    service = RuleCreationService(tmp_path, group, store=store)
    before = len(store.list_rule_match_feedbacks())
    context = EffectiveAgentContext(
        agent_instance_id="agent-a", share_group_id=group,
        project_ref=str(tmp_path / "projectA"), session_id="s1",
    )
    result = service.submit_feedback(
        "receipt-1", "exception", "agent-a",
        evidence="   ", effective_context=context,
    )
    assert result.status == "blocked"
    assert result.blocked_reason == "exception_override_body_required"
    assert len(store.list_rule_match_feedbacks()) == before
    assert not store.list_rule_exceptions(parent_rule="parent")


def test_scope_evaluation_ledger_tracks_current_outcome(tmp_path):
    from memoryguard.rule_creation import RuleCreationService
    from memoryguard.shared_memory_store import SharedMemoryStore

    group = "g1"
    store = SharedMemoryStore(tmp_path, group)
    store.append_record(
        _record(store, "parent"), assignments=[{"target_type": "agent", "target_id": "agent-a"}],
    )
    for idx in (1, 2, 3):
        store.append_rule_match_receipt(RuleMatchReceipt(
            receipt_id=f"receipt-{idx}", memory_id="parent", share_group_id=group,
            agent_instance_id="agent-a", task_hash=f"t{idx}", task="task",
            project_ref=str(tmp_path / "projectA"), session_id=f"s{idx}",
            created_at=_now_iso(),
        ))
    service = RuleCreationService(tmp_path, group, store=store)
    context = EffectiveAgentContext(
        agent_instance_id="agent-a", share_group_id=group,
        project_ref=str(tmp_path / "projectA"), session_id="s1",
    )
    # Three independent not_applicable receipts -> 3 wrong_scope conclusions.
    for idx in (1, 2, 3):
        result = service.submit_feedback(
            f"receipt-{idx}", "not_applicable", "agent-a",
            evidence="na", effective_context=context,
        )
        assert result.status != "blocked"
    stats = store.get_rule_scope_stats(
        "parent", agent_instance_id="agent-a", project_ref=str(tmp_path / "projectA"),
    )
    assert stats.wrong_scope == 3
    assert stats.accepted == 0
    # A later higher-authority followed event on receipt-1 replaces the
    # earlier not_applicable conclusion: current ledger = 2 wrong + 1 accepted.
    result = service.submit_feedback(
        "receipt-1", "followed", "agent-a",
        evidence="followed", effective_context=context,
    )
    assert result.status == "recorded"
    stats = store.get_rule_scope_stats(
        "parent", agent_instance_id="agent-a", project_ref=str(tmp_path / "projectA"),
    )
    assert stats.wrong_scope == 2
    assert stats.accepted == 1


def test_mandatory_semantic_duplicate_proposes_low_confidence(tmp_path):
    from memoryguard.governance_engine import GovernanceEngine
    from memoryguard.schema_v3 import MemoryEvent
    from memoryguard.shared_memory_store import SharedMemoryStore

    from memoryguard.schema_v3 import MemoryKind

    store = SharedMemoryStore(tmp_path, "g1")
    store.append_record(
        _record(store, "existing", body="所有 agent 默认启用 rtk 输出。",
                kind=MemoryKind.FACT),
        assignments=[{"target_type": "agent", "target_id": "agent-a"}],
    )
    engine = GovernanceEngine(tmp_path, "g1", store=store)
    event = MemoryEvent(
        event_id="event-sem", agent_instance_id="agent-a", share_group_id="g1",
        raw_content="所有 agent 默认采用 rtk 输出。",
        created_at=_now_iso(),
    )
    result = engine.auto_write(
        event,
        injection_policy="always",
        rule_assignments=[{"target_type": "agent", "target_id": "agent-a"}],
    )
    # A near/semantic duplicate of an existing mandatory rule must never fall
    # back to the legacy non-atomic organizer merge; it is surfaced as a
    # low-confidence proposal with its own decision, and the original rule is
    # left untouched until a human confirms the merge.
    assert result["mutation_kind"] == "proposed"
    assert result["record"]["status"] == "low_confidence"
    assert store.get_record("existing").status.value == "active"


def test_mandatory_rule_supersedes_related_relevant_preference(tmp_path):
    from memoryguard.governance_engine import GovernanceEngine
    from memoryguard.schema_v3 import (
        MemoryEvent, MemoryKind, SharedMemoryRecord, SharedMemoryStatus, _now_iso,
    )
    from memoryguard.shared_memory_store import SharedMemoryStore

    store = SharedMemoryStore(tmp_path, "g1")
    store.append_record(SharedMemoryRecord(
        memory_id="existing",
        body="用户偏好：默认使用 caveman 和 RTK，子代理也默认遵循。",
        kind=MemoryKind.PREFERENCE, status=SharedMemoryStatus.ACTIVE,
        confidence=0.72, injection_policy="relevant",
        created_at=_now_iso(), updated_at=_now_iso(),
    ))
    store.append_record(SharedMemoryRecord(
        memory_id="unrelated",
        body="用户长期文档偏好：先给结论，使用清晰中文、紧凑表格和可执行里程碑。",
        kind=MemoryKind.PREFERENCE, status=SharedMemoryStatus.ACTIVE,
        confidence=0.72, injection_policy="relevant",
        created_at=_now_iso(), updated_at=_now_iso(),
    ))
    store.append_record(SharedMemoryRecord(
        memory_id="project-summary",
        body="MemoryGuard 自动治理审查结论：已实现 Agent 分层与 bootstrap 注入，尚未实现会话增量自压缩。",
        kind=MemoryKind.PROJECT, status=SharedMemoryStatus.ACTIVE,
        confidence=0.72, injection_policy="relevant",
        created_at=_now_iso(), updated_at=_now_iso(),
    ))
    engine = GovernanceEngine(tmp_path, "g1", store=store)
    event = MemoryEvent(
        event_id="event-mandatory-merge", agent_instance_id="agent-a",
        share_group_id="g1",
        raw_content="全局默认使用 caveman 和 RTK，主 Agent 与所有子代理也默认遵循。",
        created_at=_now_iso(),
    )
    result = engine.auto_write(
        event,
        injection_policy="always",
        rule_assignments=[{"target_type": "agent", "target_id": "agent-a"}],
    )
    assert result["mutation_kind"] == "superseded"
    new_record = store.get_record(result["memory_id"])
    assert new_record is not None
    assert new_record.injection_policy == "always"
    assert "existing" in new_record.supersedes
    assert "unrelated" not in new_record.supersedes
    assert "project-summary" not in new_record.supersedes
    assert store.get_record("existing").status == SharedMemoryStatus.SHADOWED
    assert store.get_record("unrelated").status == SharedMemoryStatus.ACTIVE
    assert store.get_record("project-summary").status == SharedMemoryStatus.ACTIVE


def test_rule_cockpit_service_unavailable_is_fail_closed(tmp_path, monkeypatch):
    from memoryguard.gui import GovernanceApi
    from memoryguard.agent_binding import AgentBindingStore

    AgentBindingStore(tmp_path).bind_agent("agent-a", "g1")
    monkeypatch.setitem(sys.modules, "memoryguard.rule_creation", None)
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    monkeypatch.setenv("MEMORYGUARD_SHARE_GROUP_ID", "g1")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", str(tmp_path / "project"))
    api = _make_api(tmp_path)
    result = api.create_rule_from_text(
        "一句话规则",
        context={"agent_instance_id": "agent-a", "share_group_id": "g1"},
        confirmed=True,
    )
    assert result["error"] in {"service_unavailable", "service_method_unavailable"}
    undo = api.undo_rule_decision("decision-1", "g1", confirmed=True)
    assert undo["error"] == "service_unavailable"


def test_gui_feedback_fallback_is_user_authority_and_checks_receipt_owner(monkeypatch):
    from memoryguard.gui import GovernanceApi

    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    monkeypatch.setenv("MEMORYGUARD_SHARE_GROUP_ID", "g1")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", "project-a")
    receipt = types.SimpleNamespace(
        receipt_id="receipt-1", share_group_id="g1", agent_instance_id="agent-a",
        project_ref="project-a", provider="", runtime_role="", context_hash="",
        session_id="s1",
    )

    class _FallbackStore:
        group_id = "g1"

        def get_rule_match_receipt(self, receipt_id):
            return receipt if receipt_id == receipt.receipt_id else None

        def get_effective_rule_match_feedback(self, receipt_id):
            return None

        def append_rule_match_feedback(self, feedback):
            self.feedback = feedback
            return feedback

    store = _FallbackStore()
    api = GovernanceApi("unused")
    api._rule_scope_options = lambda _group: {
        "agents": [{"id": "agent-a", "label": "agent-a"}],
        "groups": [{"id": "g1", "label": "g1"}],
        "projects": [{"id": "project-a", "label": "project-a"}],
        "providers": [], "runtime_roles": [],
    }
    api._rule_bridge_service = lambda *args, **kwargs: None
    api._open_store = lambda _group, must_exist=False: (store, "")
    api._trusted_rule_bridge_context = lambda: (
        EffectiveAgentContext(
            agent_instance_id="agent-a", share_group_id="g1", project_ref="project-a",
        ),
        None,
    )

    result = api.submit_rule_feedback("receipt-1", "followed", "attacker", "evidence", "g1")
    assert result["ok"] is True
    assert result["source"] == "user"
    assert result["authority"] == 4
    assert store.feedback.actor == "user"
    assert store.feedback.source == "user"
    assert store.feedback.authority == 4

    receipt.agent_instance_id = "agent-b"
    denied = api.submit_rule_feedback("receipt-1", "followed", "attacker", "evidence", "g1")
    assert denied["error"] == "feedback_agent_does_not_own_receipt"


def test_gui_lifecycle_feedback_never_falls_back_to_plain_store(monkeypatch):
    from memoryguard.gui import GovernanceApi

    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    monkeypatch.setenv("MEMORYGUARD_SHARE_GROUP_ID", "g1")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", "project-a")

    class _Store:
        group_id = "g1"

        def get_rule_match_receipt(self, receipt_id):
            return types.SimpleNamespace(
                receipt_id=receipt_id, share_group_id="g1",
                agent_instance_id="agent-a", project_ref="project-a",
                provider="", runtime_role="", context_hash="", session_id="s1",
            )

        def get_effective_rule_match_feedback(self, receipt_id):
            return None

        def append_rule_match_feedback(self, feedback):
            raise AssertionError("lifecycle feedback must not hit plain-store fallback")

    store = _Store()
    api = GovernanceApi("unused")
    api._rule_bridge_service = lambda *args, **kwargs: None
    api._open_store = lambda _group, must_exist=False: (store, "")
    api._trusted_rule_bridge_context = lambda: (
        EffectiveAgentContext(
            agent_instance_id="agent-a", share_group_id="g1", project_ref="project-a",
        ),
        None,
    )
    for lifecycle_outcome in ("not_applicable", "exception"):
        result = api.submit_rule_feedback(
            "receipt-1", lifecycle_outcome, "agent-a", "evidence", "g1",
        )
        assert result["error"] == "lifecycle_feedback_requires_rule_service"


def test_interactive_rule_cockpit_surface_is_present():
    from memoryguard.interactive import render_interactive_html

    html = render_interactive_html()
    for marker in (
        "createRuleFromText", "自动范围决策", "撤销自动决定",
        "命中回执与反馈", "新增子例外", "submitRuleFeedback",
    ):
        assert marker in html


def _record(store, mid: str, body: str = "所有 agent 都采用同一套格式。",
            kind=None):
    from memoryguard.schema_v3 import (
        MemoryKind, SharedMemoryRecord, SharedMemoryStatus,
    )

    return SharedMemoryRecord(
        memory_id=mid, body=body,
        kind=kind or MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE, agent_instance_id="agent-a",
        injection_policy="always", priority=10,
        created_at=_now_iso(), updated_at=_now_iso(),
    )
