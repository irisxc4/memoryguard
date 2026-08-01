from pathlib import Path

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.rule_creation import RuleCreationService
from memoryguard.schema_v3 import (
    EffectiveAgentContext,
    MemoryKind,
    RuleMatchReceipt,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
)
from memoryguard.shared_memory_store import SharedMemoryStore


def _parent(tmp_path):
    AgentBindingStore(tmp_path).bind_agent("a", "team")
    store = SharedMemoryStore(tmp_path, "team")
    store.append_record(SharedMemoryRecord(
        memory_id="parent", body="始终先运行测试", kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE, injection_policy="always",
        agent_instance_id="a",
    ), assignments=[
        {"target_type": "agent", "target_id": "a"},
        {"target_type": "agent_project", "target_id": "a", "project_ref": str(tmp_path / "other"), "effect": "exclude"},
    ])
    return store


def _context(tmp_path, session):
    return EffectiveAgentContext(
        agent_instance_id="a", share_group_id="team",
        project_ref=str(tmp_path / "project"), session_id=session,
    )


def _receipt(store, tmp_path, receipt_id, session):
    store.append_rule_match_receipt(RuleMatchReceipt(
        receipt_id=receipt_id, memory_id="parent", share_group_id="team",
        agent_instance_id="a", task_hash=receipt_id, task="task",
        project_ref=str(tmp_path / "project"), session_id=session,
        created_at=_now_iso(),
    ))


def test_narrowing_aggregates_receipts_sessions_and_preserves_excludes(tmp_path):
    store = _parent(tmp_path)
    service = RuleCreationService(tmp_path, "team", store=store)
    for index, session in enumerate(("s1", "s2", "s3"), start=1):
        receipt_id = f"r{index}"
        _receipt(store, tmp_path, receipt_id, session)
        decision = service.submit_feedback(
            receipt_id, "not_applicable", f"agent-{index}",
            effective_context=_context(tmp_path, session),
        )
        if index < 3:
            assert decision.status == "pending"
        else:
            assert decision.status == "created"
            assert decision.action == "rule_narrow"
    assert store.get_record("parent") is not None
    assert not any(item.action == "rule_narrow" and item.memory_id != "parent" for item in store.list_rule_decisions())
    assignments = store.list_rule_assignments("parent")
    assert any(item.effect == "exclude" and Path(item.project_ref).name == "project" for item in assignments)
    assert any(item.effect == "exclude" and Path(item.project_ref).name == "other" for item in assignments)


def test_followed_evidence_blocks_narrowing(tmp_path):
    store = _parent(tmp_path)
    service = RuleCreationService(tmp_path, "team", store=store)
    _receipt(store, tmp_path, "rf", "followed-session")
    followed = service.submit_feedback(
        "rf", "followed", "agent-followed",
        effective_context=_context(tmp_path, "followed-session"),
    )
    assert followed.status == "recorded"
    for index, session in enumerate(("s1", "s2", "s3"), start=1):
        receipt_id = f"rn{index}"
        _receipt(store, tmp_path, receipt_id, session)
        decision = service.submit_feedback(
            receipt_id, "not_applicable", f"agent-{index}",
            effective_context=_context(tmp_path, session),
        )
    assert decision.status == "pending"
    assert decision.after["metadata"]["opposed_followed_count"] == 1
    project_ref = str(tmp_path / "project").replace("\\", "/").lower()
    assert not any(
        item.effect == "exclude" and str(item.project_ref).replace("\\", "/").lower() == project_ref
        for item in store.list_rule_assignments("parent")
    )


def test_exception_revoke_restores_parent_and_child_behavior(tmp_path):
    store = _parent(tmp_path)
    _receipt(store, tmp_path, "exception-receipt", "exception-session")
    service = RuleCreationService(tmp_path, "team", store=store)
    context = _context(tmp_path, "exception-session")
    decision = service.submit_feedback(
        "exception-receipt", "exception", "display-user",
        evidence="当前项目允许跳过测试", effective_context=context,
        producer="user",
    )
    assert decision.status == "created"
    relation = store.list_rule_exceptions(parent_rule="parent")[0]
    result = service.revoke_exception(relation.exception_id)
    assert result["ok"] is True
    assert result["active"] is False
    assert store.get_record(relation.child_exception).status == SharedMemoryStatus.DELETED
    assert any(item.action == "rule_exception_revoke" for item in store.list_rule_decisions())
    assignments = store.list_rule_assignments("parent")
    assert not any(item.effect == "exclude" and Path(item.project_ref).name == "project" for item in assignments)
    assert any(item.effect == "exclude" and Path(item.project_ref).name == "other" for item in assignments)


def test_exception_revoke_keeps_preexisting_target_exclude(tmp_path):
    store = _parent(tmp_path)
    project_ref = str(tmp_path / "project")
    existing = [item.to_dict() for item in store.list_rule_assignments("parent")]
    existing.append({
        "target_type": "agent_project", "target_id": "a",
        "project_ref": project_ref, "effect": "exclude",
    })
    store.set_rule_assignments("parent", existing, automatic=True, actor_agent_id="a")
    _receipt(store, tmp_path, "preexisting-exclude", "preexisting-session")
    service = RuleCreationService(tmp_path, "team", store=store)
    decision = service.submit_feedback(
        "preexisting-exclude", "exception", "agent-a",
        evidence="explicit alternate procedure", effective_context=_context(tmp_path, "preexisting-session"),
    )
    assert decision.status == "created"
    relation = store.list_rule_exceptions(parent_rule="parent")[0]
    assert service.revoke_exception(relation.exception_id)["ok"] is True
    assignments = store.list_rule_assignments("parent")
    assert any(
        item.effect == "exclude" and item.target_type == "agent_project"
        and Path(item.project_ref).name == "project"
        for item in assignments
    )


def test_exception_revoke_keeps_exclude_while_sibling_active(tmp_path):
    store = _parent(tmp_path)
    service = RuleCreationService(tmp_path, "team", store=store)
    for receipt_id, session in (("sibling-1", "s1"), ("sibling-2", "s2")):
        _receipt(store, tmp_path, receipt_id, session)
        decision = service.submit_feedback(
            receipt_id, "exception", f"agent-{session}",
            evidence=f"alternate procedure {session}",
            effective_context=_context(tmp_path, session),
        )
        assert decision.status == "created"
    relations = store.list_rule_exceptions(parent_rule="parent")
    assert len(relations) == 2
    assert service.revoke_exception(relations[0].exception_id)["ok"] is True
    assert any(
        item.effect == "exclude" and Path(item.project_ref).name == "project"
        for item in store.list_rule_assignments("parent")
    )


def test_target_undo_requires_structured_inverse_and_never_group_snapshot(tmp_path):
    store = _parent(tmp_path)
    service = RuleCreationService(tmp_path, "team", store=store)
    decision = service.create_rule_from_text(
        "自动创建的规则", _context(tmp_path, "undo-session"),
        requested_scope={"target_type": "agent", "target_id": "a"},
    )
    assert decision.status == "created"
    undone = service.undo_rule_decision(
        decision.decision_id, _context(tmp_path, "undo-session"),
    )
    assert undone.status == "undone"
    assert store.get_record(decision.memory_id).status == SharedMemoryStatus.DELETED
    missing = service.undo_rule("missing-snapshot", _context(tmp_path, "undo-session"))
    assert missing.status == "blocked"
    assert missing.blocked_reason == "structured_decision_required"
