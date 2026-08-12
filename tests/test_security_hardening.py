"""V2_ACTIVE security, privacy, path-safety, and concurrency assertions."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtom, MemoryAtomStore, MemoryReadScope
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.storage.layout import LayoutError, WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState


def _activate_v2(root: Path) -> tuple[MemoryAtomStore, EvidenceStore, GovernanceV2, GroupControlService]:
    initialize_all(WorkspaceV2Layout(root))
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    manager = ManifestManager(root)
    manager.transition(ManifestState.V2_BUILDING, migration_id="security-hardening")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="security-source",
        target_digest="security-target",
        manifest_digest="security-manifest",
        digests={"validator_passed": True, "checkpoints": {"core": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE
    return memory, evidence, governance, GroupControlService(root, write=True)


def _mutation_context(root: Path, agent: str, group: str, *, admin: bool = True) -> V2MutationContext:
    workspace = str(root.resolve())
    return V2MutationContext(
        workspace_id=workspace,
        share_group_id=group,
        agent_instance_id=agent,
        project_ref=workspace,
        provider="codex",
        runtime_role="test",
        actor=agent,
        authority="admin" if admin else "manual",
        admin=admin,
    )


def _native_context(root: Path, agent: str, group: str, *, admin: bool = False):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id=f"security-{agent}",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(root.resolve()),
        share_group_id=group,
        project_ref=str(root.resolve()),
        provider="codex",
        runtime_role="test",
        entrypoint="security-test",
    )


def _port(root: Path) -> NativeV2RuntimePort:
    return NativeV2RuntimePort(
        root,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 1},
    )


def test_cross_group_access_denied(tmp_path: Path):
    """Agent B cannot read Agent A's group or impersonate its identity."""
    _memory, _evidence, _governance, groups = _activate_v2(tmp_path)
    groups.bind_agent("agent-a", "group-a", idempotency_key="bind-a")
    groups.bind_agent("agent-b", "group-b", idempotency_key="bind-b")
    port = _port(tmp_path)

    written = port.dispatch_mcp(
        "memoryguard_memory_write",
        {
            "memory_id": "agent-a-memory",
            "body": "agent-a 的私有记忆",
            "event_id": "security-agent-a",
            "idempotency_key": "security-agent-a-write",
            "evidence": [{"source_ref": "security/agent-a", "digest": "a"}],
        },
        context=_native_context(tmp_path, "agent-a", "group-a"),
        generation=1,
        state="V2_ACTIVE",
    )
    assert written["ok"] is True, written

    impersonated = port.dispatch_mcp(
        "memoryguard_memory_read",
        {"memory_id": "agent-a-memory", "agent_instance_id": "agent-a"},
        context=_native_context(tmp_path, "agent-b", "group-b"),
        generation=1,
        state="V2_ACTIVE",
    )
    assert impersonated["ok"] is False, impersonated

    search = port.dispatch_mcp(
        "memoryguard_memory_search",
        {"query": "私有"},
        context=_native_context(tmp_path, "agent-b", "group-b"),
        generation=1,
        state="V2_ACTIVE",
    )
    assert search["ok"] is True, search
    assert search["data"] == []


def test_readonly_no_side_effects(tmp_path: Path):
    """Readonly V2 stores neither create nor mutate persistent state."""
    memory, evidence, governance, _groups = _activate_v2(tmp_path)
    group = "readonly-group"
    context = _mutation_context(tmp_path, "readonly-agent", group)
    atom, _decision = governance.put_atom(
        MemoryAtom(
            memory_id="rec1",
            body="test content",
            kind="fact",
            status="active",
            share_group_id=group,
            agent_instance_id=context.agent_instance_id,
            project_ref=context.project_ref,
            workspace_id=context.workspace_id,
            visibility="building",
        ),
        context=context,
        evidence=[{"source_ref": "security/readonly", "digest": "readonly"}],
        reason="readonly fixture",
        idempotency_key="readonly-seed",
    )
    memory.project_evidence(evidence)
    memory.set_visibility("active", atom_ids=[atom.atom_id])

    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and path.name not in {"memory.db-shm", "memory.db-wal", "memory.db-journal"}
    }

    readonly = MemoryAtomStore(tmp_path, readonly=True)
    scope = MemoryReadScope(
        workspace_id=str(tmp_path.resolve()),
        share_group_id=group,
        admin=True,
    )
    assert [item.memory_id for item in readonly.list_atoms(scope=scope)] == ["rec1"]
    with pytest.raises(PermissionError):
        readonly.put_atom(
            MemoryAtom(memory_id="rec2", body="should fail", share_group_id=group),
            context=context,
        )

    missing = tmp_path / "missing-readonly"
    with pytest.raises(FileNotFoundError):
        MemoryAtomStore(missing, readonly=True)

    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and path.name not in {"memory.db-shm", "memory.db-wal", "memory.db-journal"}
    }
    assert before == after


def test_update_secret_redacted(tmp_path: Path):
    """The V2 GUI update path redacts secrets before durable revision writes."""
    memory, _evidence, governance, _groups = _activate_v2(tmp_path)
    group = "update-secret-group"
    context = _mutation_context(tmp_path, "agent-a", group)
    atom, _decision = governance.put_atom(
        MemoryAtom(
            memory_id="secret-target",
            body="正常记忆内容用于测试更新",
            share_group_id=group,
            agent_instance_id="agent-a",
            project_ref=context.project_ref,
            workspace_id=context.workspace_id,
            visibility="building",
        ),
        context=context,
        evidence=[{"source_ref": "security/update", "digest": "update"}],
        reason="update fixture",
        idempotency_key="update-seed",
    )
    memory.project_evidence(EvidenceStore(tmp_path))
    memory.set_visibility("active", atom_ids=[atom.atom_id])

    result = _port(tmp_path).dispatch_gui(
        "edit_memory",
        {"memory_id": "secret-target", "body": "api_key=sk-update123def456ghi789jkl012mno345pqr789"},
        context=_native_context(tmp_path, "agent-a", group, admin=True),
        generation=1,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True, result

    stored = memory.get_atom(
        "secret-target",
        scope=MemoryReadScope(
            workspace_id=str(tmp_path.resolve()),
            share_group_id=group,
            admin=True,
        ),
        include_building=True,
    )
    assert stored is not None
    assert "sk-update123" not in stored.body
    assert "[REDACTED]" in stored.body
    for path in (tmp_path / ".memoryguard").rglob("*"):
        if path.is_file():
            assert b"sk-update123" not in path.read_bytes(), path


def test_concurrent_same_body_one_record(tmp_path: Path):
    """N native V2 writes of one body yield one atom and N provenance rows."""
    memory, _evidence, _governance, groups = _activate_v2(tmp_path)
    group = "concurrent-group"
    agents = [f"agent-{index}" for index in range(10)]
    for agent in agents:
        groups.bind_agent(agent, group, idempotency_key=f"bind-{agent}")
    port = _port(tmp_path)
    # Prime the native port's lazy memory/evidence/governance leases before
    # several writers enter the same process concurrently.  The production
    # path remains lazy; this fixture makes the contention under test the
    # organizer's body lock and governed dedup transaction, not first-open
    # schema initialization.
    warmup = port.dispatch_mcp(
        "memoryguard_memory_write",
        {
            "memory_id": "concurrency-warmup",
            "body": "warmup",
            "event_id": "concurrency-warmup",
            "idempotency_key": "concurrency-warmup",
            "evidence": [{"source_ref": "security/warmup", "digest": "warmup"}],
        },
        context=_native_context(tmp_path, agents[0], group),
        generation=1,
        state="V2_ACTIVE",
    )
    assert warmup["ok"] is True, warmup
    barrier = threading.Barrier(len(agents))
    errors: list[str] = []

    def write_one(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            result = port.dispatch_mcp(
                "memoryguard_memory_write",
                {
                    "memory_id": f"rec-{index}",
                    "body": "完全相同的并发写入测试内容",
                    "event_id": f"concurrent-event-{index}",
                    "idempotency_key": f"concurrent-write-{index}",
                    "evidence": [{"source_ref": f"security/concurrent/{index}", "digest": f"hash-{index}"}],
                },
                context=_native_context(tmp_path, agents[index], group),
                generation=1,
                state="V2_ACTIVE",
            )
            if result.get("ok") is not True:
                errors.append(str(result))
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(repr(exc))

    threads = [threading.Thread(target=write_one, args=(index,)) for index in range(len(agents))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    scope = MemoryReadScope(
        workspace_id=str(tmp_path.resolve()),
        share_group_id=group,
        admin=True,
    )
    same_body = [
        item for item in memory.list_atoms(scope=scope, include_building=True)
        if item.body == "完全相同的并发写入测试内容" and item.status == "active"
    ]
    assert not errors, errors
    assert len(same_body) == 1, same_body
    assert len(same_body[0].provenance) == len(agents)


def test_path_traversal_blocked(tmp_path: Path):
    """V2 database paths are exact layout members; traversal cannot open them."""
    layout = WorkspaceV2Layout(tmp_path)
    outside = tmp_path / "outside.sqlite"
    with pytest.raises(LayoutError):
        layout.assert_database_path(outside, "memory")
    with pytest.raises(ValueError):
        MemoryAtomStore(path=outside, workspace_or_path=tmp_path)
    with pytest.raises(ValueError):
        EvidenceStore(path=outside, workspace_or_path=tmp_path)

    escaped = tmp_path / ".memoryguard" / "memory" / ".." / "escaped.db"
    with pytest.raises(LayoutError):
        layout.assert_database_path(escaped, "memory")
    assert not outside.exists()
    assert not (tmp_path / "escaped.db").exists()


def test_access_context_impersonation_blocked(monkeypatch):
    """AccessContext rejects an identity claim that differs from its binding."""
    from memoryguard.access_context import load_access_context

    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "trusted-agent")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    ctx = load_access_context()

    ok, _err = ctx.check_agent("trusted-agent")
    assert ok
    ok, err = ctx.check_agent("impostor")
    assert not ok
    assert "mismatch" in err


def test_strict_binding_default_on(monkeypatch):
    """STRICT_BINDING defaults to enabled."""
    from memoryguard.access_context import load_access_context

    monkeypatch.delenv("MEMORYGUARD_STRICT_BINDING", raising=False)
    ctx = load_access_context()
    assert ctx.strict_binding
