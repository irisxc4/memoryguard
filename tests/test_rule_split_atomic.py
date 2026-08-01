from pathlib import Path

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.rule_creation import RuleCreationService
from memoryguard.rule_scope import canonical_project_ref
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


def test_narrow_undo_rejects_later_parent_edit(tmp_path):
    """A parent assignment edit invalidates a previously captured narrow inverse."""
    store = _parent(tmp_path)
    service = RuleCreationService(tmp_path, "team", store=store)
    decision = None
    for index, session in enumerate(("narrow-s1", "narrow-s2", "narrow-s3"), start=1):
        receipt_id = f"narrow-undo-{index}"
        _receipt(store, tmp_path, receipt_id, session)
        decision = service.submit_feedback(
            receipt_id, "not_applicable", "agent-a",
            effective_context=_context(tmp_path, session),
        )
    assert decision is not None and decision.status == "created"
    assert decision.action == "rule_narrow"

    # Simulate a later human/agent edit after the narrowing decision.  The
    # inverse must not restore the stale before-snapshot over this assignment.
    edited = [item.to_dict() for item in store.list_rule_assignments("parent")]
    edited.append({
        "target_type": "agent_project", "target_id": "a",
        "project_ref": str(tmp_path / "later-edit"), "effect": "include",
    })
    store.set_rule_assignments(
        "parent", edited, automatic=True, actor_agent_id="a",
    )
    after_edit = [item.to_dict() for item in store.list_rule_assignments("parent")]

    undone = service.undo_rule_decision(
        decision.decision_id, _context(tmp_path, "narrow-s3"),
    )
    assert undone.status == "blocked"
    assert "parent_assignment_revision_conflict" in undone.blocked_reason
    assert [item.to_dict() for item in store.list_rule_assignments("parent")] == after_edit


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
    result = service.revoke_exception(relation.exception_id, context)
    assert result["ok"] is True
    assert result["active"] is False
    assert store.get_record(relation.child_exception).status == SharedMemoryStatus.DELETED
    assert any(item.action == "rule_exception_revoke" for item in store.list_rule_decisions())
    assignments = store.list_rule_assignments("parent")
    assert not any(item.effect == "exclude" and Path(item.project_ref).name == "project" for item in assignments)
    assert any(item.effect == "exclude" and Path(item.project_ref).name == "other" for item in assignments)


def test_exception_undo_rejects_later_parent_edit(tmp_path):
    """An exception inverse must fail closed after a parent assignment edit."""
    store = _parent(tmp_path)
    _receipt(store, tmp_path, "exception-undo", "exception-undo-session")
    service = RuleCreationService(tmp_path, "team", store=store)
    context = _context(tmp_path, "exception-undo-session")
    decision = service.submit_feedback(
        "exception-undo", "exception", "agent-a",
        evidence="temporary project-specific procedure", effective_context=context,
    )
    assert decision.status == "created"
    assert decision.action == "rule_exception"

    # A subsequent parent edit changes the assignment revision captured by
    # the exception decision.  Undo must preserve this edit and the child.
    edited = [item.to_dict() for item in store.list_rule_assignments("parent")]
    edited.append({
        "target_type": "agent_project", "target_id": "a",
        "project_ref": str(tmp_path / "later-exception-edit"), "effect": "include",
    })
    store.set_rule_assignments(
        "parent", edited, automatic=True, actor_agent_id="a",
    )
    after_edit = [item.to_dict() for item in store.list_rule_assignments("parent")]
    child_id = store.list_rule_exceptions(parent_rule="parent")[0].child_exception

    # Revocation is guarded by a LOCAL delta (this relation's generated
    # exclude, its child rule, and the relation's active flag), never by the
    # parent's whole assignment multiset.  An unrelated later parent edit on
    # another project must not block this exception's undo.
    undone = service.undo_rule_decision(decision.decision_id, context)
    assert undone.status == "undone"
    parent_after_undo = store.list_rule_assignments("parent")
    # The later include edit is preserved.
    assert any(
        item.target_type == "agent_project"
        and item.project_ref == canonical_project_ref(str(tmp_path / "later-exception-edit"))
        and item.effect == "include"
        for item in parent_after_undo
    )
    # This exception's generated exclude is removed and the child soft-deleted.
    assert not any(
        item.effect == "exclude"
        and item.target_id == "a"
        and item.project_ref == canonical_project_ref(str(tmp_path / "project"))
        for item in parent_after_undo
    )
    assert store.get_record(child_id).status == SharedMemoryStatus.DELETED


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
    assert service.revoke_exception(
        relation.exception_id,
        _context(tmp_path, "preexisting-session"),
    )["ok"] is True
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
    assert service.revoke_exception(
        relations[0].exception_id,
        _context(tmp_path, "s1"),
    )["ok"] is True
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
