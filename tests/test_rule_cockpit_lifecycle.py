"""Rule cockpit bridge lifecycle tests.

These tests exercise the GUI boundary with a tiny fake lifecycle service.  The
real service is optional during rolling upgrades; the bridge must fail closed
and must never infer a system/cross-Agent audience from a sentence.
"""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class _FakeRuleCreationService:
    def __init__(self, workspace, share_group_id="default"):
        self.workspace = workspace
        self.share_group_id = share_group_id
        self.calls = []

    def create_rule_from_text(self, text, context):
        self.calls.append(("create", text, context))
        return {
            "ok": True,
            "rule_id": "rule-1",
            "memory_id": "rule-1",
            "kind": "procedure",
            "assignments": [{
                "target_type": "agent_project",
                "target_id": context.agent_instance_id,
                "project_ref": context.project_ref,
                "effect": "include",
            }],
            "scope_confidence": 0.42,
            "scope_reason": "当前 Agent + 项目候选；未扩大到共享组",
            "decision_id": "decision-1",
            "undo_id": "undo-1",
        }

    def list_decisions(self, limit=50):
        return {"decisions": [{
            "decision_id": "decision-1", "rule_id": "rule-1",
            "scope_confidence": 0.42, "scope_reason": "narrow candidate",
            "undo_id": "undo-1", "status": "active",
        }], "total": 1}

    def undo_rule_decision(self, decision_id, context=None):
        return {"ok": True, "decision_id": decision_id, "status": "undone"}

    def scope_stats(self):
        return {"stats": [{"rule_id": "rule-1", "total": 2, "accepted": 1,
                            "corrected": 1, "wrong_scope": 0}],
                "auto_scope": {"total": 1, "low_confidence": 1}}

    def submit_feedback(self, receipt_id, outcome, actor, evidence=""):
        return {"ok": True, "receipt_id": receipt_id, "outcome": outcome,
                "actor": actor, "evidence": evidence}

    def list_exceptions(self, parent_rule=""):
        return {"exceptions": [{"exception_id": "ex-1", "parent_rule": parent_rule or "rule-1",
                                "child_exception": "rule-child", "priority": 10,
                                "reason": "explicit exception", "active": True}], "total": 1}

    def create_rule_exception(self, parent_rule, child_exception, priority=0, reason=""):
        return {"ok": True, "exception_id": "ex-1", "parent_rule": parent_rule,
                "child_exception": child_exception, "priority": priority, "reason": reason}

    def revoke_exception(self, exception_id):
        return {"ok": True, "exception_id": exception_id, "active": False}


@pytest.fixture()
def fake_service(monkeypatch):
    module = types.ModuleType("memoryguard.rule_creation")
    module.RuleCreationService = _FakeRuleCreationService
    monkeypatch.setitem(sys.modules, "memoryguard.rule_creation", module)
    return module


def test_rule_cockpit_create_requires_explicit_context_and_preserves_narrow_scope(fake_service):
    from memoryguard.gui import GovernanceApi

    with tempfile.TemporaryDirectory() as workspace:
        api = GovernanceApi(workspace)
        api._rule_scope_options = lambda _group: {
            "agents": [{"id": "agent-a", "label": "agent-a"}],
            "groups": [{"id": "g1", "label": "g1"}],
            "projects": [{"id": "project-a", "label": "project-a"}],
            "providers": [], "runtime_roles": [],
        }
        missing = api.create_rule_from_text("一句话规则", share_group_id="g1", confirmed=True)
        assert missing["error"] == "agent_context_required"

        created = api.create_rule_from_text(
            "一句话规则",
            context={
                "agent_instance_id": "agent-a",
                "share_group_id": "g1",
                "project_ref": "project-a",
            },
            confirmed=True,
        )
        assert created["ok"] is True
        assert created["rule_id"] == "rule-1"
        assert created["scope_confidence"] == pytest.approx(0.42)
        assert created["decision_id"] == "decision-1"
        assert created["undo_id"] == "undo-1"
        assert created["assignments"][0]["target_type"] == "agent_project"
        assert all(item.get("target_type") != "system" for item in created["assignments"])


def test_rule_cockpit_decision_undo_feedback_exception_lifecycle(fake_service):
    from memoryguard.gui import GovernanceApi

    with tempfile.TemporaryDirectory() as workspace:
        api = GovernanceApi(workspace)
        api._rule_scope_options = lambda _group: {
            "agents": [{"id": "agent-a", "label": "agent-a"}],
            "groups": [{"id": "g1", "label": "g1"}],
            "projects": [{"id": "project-a", "label": "project-a"}],
            "providers": [], "runtime_roles": [],
        }
        decisions = api.list_rule_decisions("g1")
        assert decisions["decisions"][0]["decision_id"] == "decision-1"
        metrics = api.get_rule_auto_scope_metrics("g1")
        assert metrics["auto_scope"]["low_confidence"] == 1

        assert api.undo_rule_decision("decision-1", "g1", confirmed=False)["error"] == "confirmation_required"
        undone = api.undo_rule_decision(
            "decision-1", "g1", confirmed=True,
            context={"agent_instance_id": "agent-a", "share_group_id": "g1", "project_ref": "project-a"},
        )
        assert undone["status"] == "undone"

        feedback = api.submit_rule_feedback("receipt-1", "followed", "agent-a", "evidence", "g1")
        assert feedback["outcome"] == "followed"

        invalid = api.create_child_exception("rule-1", "rule-1", confirmed=True)
        assert invalid["error"] == "rule_exception_cannot_reference_itself"
        created = api.create_child_exception("rule-1", "rule-child", 10, "explicit exception", "g1", True)
        assert created["parent_rule"] == "rule-1"
        assert created["child_exception"] == "rule-child"
        listed = api.list_rule_exceptions("g1", "rule-1")
        assert listed["exceptions"][0]["parent_rule"] == "rule-1"
        revoked = api.revoke_rule_exception("ex-1", "g1", confirmed=True)
        assert revoked["active"] is False


def test_rule_cockpit_service_unavailable_is_fail_closed(monkeypatch):
    from memoryguard.gui import GovernanceApi

    monkeypatch.setitem(sys.modules, "memoryguard.rule_creation", None)
    with tempfile.TemporaryDirectory() as workspace:
        api = GovernanceApi(workspace)
        api._rule_scope_options = lambda _group: {
            "agents": [], "groups": [{"id": "g1", "label": "g1"}],
            "projects": [], "providers": [], "runtime_roles": [],
        }
        result = api.create_rule_from_text(
            "一句话规则",
            context={"agent_instance_id": "agent-a", "share_group_id": "g1"},
            confirmed=True,
        )
        assert result["error"] in {"service_unavailable", "service_method_unavailable"}
        undo = api.undo_rule_decision("decision-1", "g1", confirmed=True)
        assert undo["error"] == "service_unavailable"


def test_interactive_rule_cockpit_surface_is_present():
    from memoryguard.interactive import render_interactive_html

    html = render_interactive_html()
    for marker in (
        "createRuleFromText", "自动范围决策", "撤销自动决定",
        "命中回执与反馈", "新增子例外", "submitRuleFeedback",
    ):
        assert marker in html
