from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.codegraph_v2.models import CodeGraphScope
from memoryguard.codegraph_v2.store import CodeGraphStore
from memoryguard.evidence.store import EvidenceStore
from memoryguard.governance_v2.context import V2MutationContext
from memoryguard.maintenance_v2.reference_audit import ReferenceAudit
from memoryguard.memory.store import MemoryAtom, MemoryAtomStore
from memoryguard.runtime_v2.audit_plan import AuditPlanError, AuditPlanService
from memoryguard.runtime_v2.group_native import GroupControlError, SystemControlStore
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


def _context(workspace: Path):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-a",
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="audit-plan-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id="group-a",
        project_ref=str(workspace.resolve()),
        provider="gui",
        runtime_role="gui",
        entrypoint="gui",
    )


def _port(workspace: Path) -> NativeV2RuntimePort:
    return NativeV2RuntimePort(
        workspace,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 11},
    )


def _finding_id(code: str, domain: str, table: str = "") -> str:
    import hashlib
    return "v2-" + hashlib.sha256(
        f"{code}\0{domain}\0{table}".encode("utf-8")
    ).hexdigest()[:16]


def test_unfixable_audit_finding_generates_plan_but_apply_refuses(tmp_path: Path) -> None:
    service = AuditPlanService(tmp_path)
    # Fresh workspace has one missing_database blocker per domain. Use the
    # runtime finding to prove generation is read-only and fail-closed.
    finding = _finding_id("missing_database", "runtime")
    before = list(tmp_path.rglob("*"))
    generated = service.generate(finding)
    plan = generated["plan"]
    assert plan["fixable"] is False
    assert plan["action"] == "manual_repair_required"
    assert list(tmp_path.rglob("*")) == before
    with pytest.raises(AuditPlanError, match="audit_plan_not_fixable"):
        service.apply(plan["plan_id"])
    assert list(tmp_path.rglob("*")) == before


def test_memory_outbox_audit_plan_projects_and_records_nonundoable_receipt(tmp_path: Path) -> None:
    SystemControlStore(tmp_path, write=True)
    evidence = EvidenceStore(tmp_path)
    memory = MemoryAtomStore(tmp_path)
    context = V2MutationContext(
        workspace_id=str(tmp_path.resolve()),
        share_group_id="group-a",
        agent_instance_id="agent-a",
        project_ref=str(tmp_path.resolve()),
        provider="gui",
        runtime_role="gui",
        actor="agent-a",
        authority="admin",
        admin=True,
    )
    memory.put_atom(
        MemoryAtom(
            memory_id="audit-outbox-memory",
            body="body remains in memory authority",
            share_group_id="group-a",
            agent_instance_id="agent-a",
            project_ref=str(tmp_path.resolve()),
            provider="gui",
            runtime_role="gui",
        ),
        context=context,
        evidence=[{
            "source_ref": "test:audit-plan",
            "digest": "digest-audit-plan",
            "authority": "audit",
        }],
    )
    before = ReferenceAudit(tmp_path).audit()
    blocker = next(
        item for item in before.blockers
        if item.code == "unconsumed_outbox" and item.domain == "memory" and item.table == "domain_outbox"
    )
    service = AuditPlanService(tmp_path)
    generated = service.generate(_finding_id(blocker.code, blocker.domain, blocker.table))
    plan = generated["plan"]
    assert plan["fixable"] is True
    assert plan["action"] == "project_memory_outbox"

    applied = service.apply(plan["plan_id"])
    assert applied["ok"] is True
    assert applied["change_id"] == plan["plan_id"]
    assert applied["undoable"] is False
    assert applied["remaining_count"] == 0
    after = ReferenceAudit(tmp_path).audit()
    assert not any(
        item.code == "unconsumed_outbox" and item.domain == "memory" and item.table == "domain_outbox"
        for item in after.blockers
    )

    replay = service.apply(plan["plan_id"])
    assert replay["replayed"] is True
    with pytest.raises(AuditPlanError, match="change_not_undoable"):
        service.undo(applied["change_id"])
    # The projection created evidence rather than deleting/replacing the memory atom.
    assert memory.get_atom("audit-outbox-memory", scope=context.to_dict(), include_building=True) is not None
    import sqlite3
    with sqlite3.connect(evidence.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] >= 1


def test_codegraph_outbox_audit_plan_projects_only_persisted_trusted_scope(tmp_path: Path) -> None:
    SystemControlStore(tmp_path, write=True)
    graph = CodeGraphStore(tmp_path)
    scope = CodeGraphScope(
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id="agent-a",
        project_ref=str(tmp_path.resolve()),
        provider="codex",
        share_group_id="group-a",
        trusted_context=True,
    )
    graph.append_outbox("source_file.upsert", "file-a", scope=scope)

    before = ReferenceAudit(tmp_path).audit()
    blocker = next(
        item for item in before.blockers
        if item.code == "unconsumed_outbox" and item.domain == "codegraph" and item.table == "outbox"
    )
    plan = AuditPlanService(tmp_path).generate(
        _finding_id(blocker.code, blocker.domain, blocker.table)
    )["plan"]
    assert plan["action"] == "project_codegraph_outbox"

    applied = AuditPlanService(tmp_path).apply(plan["plan_id"])
    assert applied["processed_count"] == 1
    assert applied["remaining_count"] == 0
    assert not any(
        item.code == "unconsumed_outbox" and item.domain == "codegraph" and item.table == "outbox"
        for item in ReferenceAudit(tmp_path).audit().blockers
    )


def test_codegraph_outbox_drain_retries_a_prior_projector_failure(tmp_path: Path) -> None:
    graph = CodeGraphStore(tmp_path)
    scope = CodeGraphScope(
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id="agent-a",
        project_ref=str(tmp_path.resolve()),
        provider="codex",
        share_group_id="group-a",
        trusted_context=True,
    )
    graph.append_outbox("source_file.upsert", "file-a", scope=scope)

    failed = graph.drain_outbox(
        scope=scope,
        projector=lambda _event: (_ for _ in ()).throw(RuntimeError("transient")),
    )
    assert failed == {"projected": 0, "failed": 1, "pending": 1}

    retried = graph.drain_outbox(scope=scope)
    assert retried == {"projected": 1, "failed": 0, "pending": 0}


def test_system_control_mutation_advances_checkpoint_and_is_audit_clean(tmp_path: Path) -> None:
    store = SystemControlStore(tmp_path, write=True)
    store.mutate(
        "audit_system_outbox",
        "system-a",
        {"operation": "audit"},
        lambda _conn: ({"ok": True}, "audit-system"),
    )

    with sqlite3.connect(store.db_path) as conn:
        event_count, maximum, checkpoint = conn.execute(
            "SELECT COUNT(*), MAX(sequence), "
            "(SELECT last_sequence FROM outbox_checkpoints WHERE domain='system') "
            "FROM group_outbox"
        ).fetchone()
    assert event_count == 1
    assert int(maximum) == int(checkpoint)
    assert not any(
        item.code == "unconsumed_outbox" and item.domain == "system" and item.table == "group_outbox"
        for item in ReferenceAudit(tmp_path).audit().blockers
    )

    replay = store.mutate(
        "audit_system_outbox",
        "system-a",
        {"operation": "audit"},
        lambda _conn: (_ for _ in ()).throw(AssertionError("replay must not apply")),
    )
    assert replay["replayed"] is True
    with sqlite3.connect(store.db_path) as conn:
        event_count_after, maximum_after, checkpoint_after = conn.execute(
            "SELECT COUNT(*), MAX(sequence), "
            "(SELECT last_sequence FROM outbox_checkpoints WHERE domain='system') "
            "FROM group_outbox"
        ).fetchone()
    assert (event_count_after, maximum_after, checkpoint_after) == (event_count, maximum, checkpoint)


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_system_outbox_pending_or_failed_remains_blocked_and_not_projectable(
    tmp_path: Path, status: str
) -> None:
    store = SystemControlStore(tmp_path, write=True)
    store.mutate(
        "audit_system_outbox",
        "system-a",
        {"operation": "audit"},
        lambda _conn: ({"ok": True}, "audit-system"),
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE group_outbox SET status=?", (status,))
        conn.commit()

    audit = ReferenceAudit(tmp_path).audit()
    assert any(
        item.code == "unconsumed_outbox"
        and item.domain == "system"
        and item.table == "group_outbox"
        for item in audit.blockers
    )
    with pytest.raises(GroupControlError, match="system_outbox_projection_pending"):
        store.project_outbox()


def test_system_outbox_audit_plan_repairs_lagging_projected_checkpoint(tmp_path: Path) -> None:
    store = SystemControlStore(tmp_path, write=True)
    store.mutate(
        "audit_system_outbox",
        "system-a",
        {"operation": "audit"},
        lambda _conn: ({"ok": True}, "audit-system"),
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE outbox_checkpoints SET last_sequence=0 WHERE domain='system'"
        )
        conn.commit()

    before = ReferenceAudit(tmp_path).audit()
    blocker = next(
        item for item in before.blockers
        if item.code == "unconsumed_outbox"
        and item.domain == "system"
        and item.table == "group_outbox"
    )
    service = AuditPlanService(tmp_path)
    plan = service.generate(_finding_id(blocker.code, blocker.domain, blocker.table))["plan"]
    assert plan["action"] == "project_system_outbox"

    applied = service.apply(plan["plan_id"])
    assert applied["remaining_count"] == 0
    assert not any(
        item.code == "unconsumed_outbox"
        and item.domain == "system"
        and item.table == "group_outbox"
        for item in ReferenceAudit(tmp_path).audit().blockers
    )
    with sqlite3.connect(store.db_path) as conn:
        maximum, checkpoint = conn.execute(
            "SELECT MAX(sequence), "
            "(SELECT last_sequence FROM outbox_checkpoints WHERE domain='system') "
            "FROM group_outbox"
        ).fetchone()
    assert int(maximum) == int(checkpoint)


def test_native_audit_plan_handlers_are_real_and_admin_gated(tmp_path: Path) -> None:
    port = _port(tmp_path)
    context = _context(tmp_path)
    finding = _finding_id("missing_database", "runtime")
    generated = port.dispatch_gui(
        "generate_plan", [finding],
        context=context, generation=11, state="V2_ACTIVE",
    )
    assert generated["ok"] is True, generated
    data = generated.get("data", generated)
    assert data["plan"]["fixable"] is False
    refused = port.dispatch_gui(
        "apply_plan", [data["plan"]["plan_id"]],
        context=context, generation=11, mutation=True, state="V2_ACTIVE",
    )
    assert refused["ok"] is False
    assert refused["code"] == "audit_plan_not_fixable"
