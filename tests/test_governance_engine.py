import json
from copy import deepcopy
import sqlite3
from dataclasses import replace
from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.auto_organizer import AutoOrganizer
from memoryguard.content import ContentStore
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.gui import GovernanceApi
from memoryguard.memory import MemoryAtom, MemoryAtomStore, MemoryReadScope
from memoryguard.mcp_server import execute_tool
from memoryguard.projection_v2.store import ProjectionStore
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.runtime_v2.working_memory import RuntimeStore
from memoryguard.schema_v3 import MemoryEvent
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState


def _activate_v2_workspace(root: Path) -> None:
    initialize_all(WorkspaceV2Layout(root))
    MemoryAtomStore(root)
    EvidenceStore(root)
    GovernanceV2(root)
    RuleV2Store(root)
    ProjectionStore(root)
    ContentStore(root)
    RuntimeStore(root)
    manager = ManifestManager(root)
    manager.transition(ManifestState.V2_BUILDING, migration_id="governance-engine-fixture")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="governance-engine-source",
        target_digest="governance-engine-target",
        manifest_digest="governance-engine-manifest",
        digests={"validator_passed": True, "checkpoints": {"governance": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE


def _configure_v2_identity(monkeypatch, root: Path, *, agent: str = "agent-a") -> None:
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(root))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", agent)
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "0")
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    monkeypatch.setenv("MEMORYGUARD_SESSION_ID", "governance-engine-session")
    monkeypatch.setenv("MEMORYGUARD_SESSION_SOURCE", "transport")
    monkeypatch.setenv("MEMORYGUARD_SESSION_TRUSTED", "1")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", str(root))
    monkeypatch.setenv("MEMORYGUARD_PROVIDER", "gui")
    monkeypatch.setenv("MEMORYGUARD_RUNTIME_ROLE", "gui")


def _seed_v2_atom(root: Path, *, group: str = "group-a") -> MemoryAtomStore:
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    scope = {
        "workspace_id": str(root.resolve()),
        "share_group_id": group,
        "agent_instance_id": "agent-a",
        "project_ref": str(root.resolve()),
        "provider": "gui",
        "runtime_role": "gui",
        "actor": "fixture",
        "authority": "manual",
    }
    atom = MemoryAtom(
        memory_id="mcp-memory",
        body="seed body",
        kind="fact",
        status="active",
        confidence=1.0,
        locked=False,
        injection_policy="relevant",
        priority=0,
        metadata={},
        workspace_id=scope["workspace_id"],
        share_group_id=group,
        agent_instance_id="agent-a",
        project_ref=scope["project_ref"],
        provider="gui",
        runtime_role="gui",
    )
    persisted, _ = governance.put_atom(
        atom,
        context=scope,
        evidence=[{"source_ref": "fixture:mcp-memory", "authority": "governance"}],
        reason="governance engine V2 fixture",
        confidence=1.0,
        idempotency_key="governance-engine-mcp-memory",
    )
    memory.project_evidence(evidence)
    memory.set_visibility("active", atom_ids=[persisted.atom_id])
    return memory


def _mcp_payload(result: dict) -> dict:
    assert result.get("isError") is not True, result
    return json.loads(result["content"][0]["text"])


def _native_context(root: Path):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-a",
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="governance-engine-native",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(root.resolve()),
        share_group_id="group-a",
        project_ref=str(root.resolve()),
        provider="gui",
        runtime_role="gui",
        entrypoint="mcp",
    )


def _v2_receipt_counts(root: Path) -> tuple[int, int, int]:
    ledger = root / ".memoryguard" / "governance_v2" / "decisions.db"
    with sqlite3.connect(ledger) as connection:
        decisions = int(connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0])
        requests = int(connection.execute("SELECT COUNT(*) FROM request_ledger").fetchone()[0])
        outbox = int(connection.execute("SELECT COUNT(*) FROM decision_outbox").fetchone()[0])
    return decisions, requests, outbox


def _v2_revisions(root: Path, memory_id: str) -> list[dict]:
    return MemoryAtomStore(root, readonly=True).list_revisions(
        scope=MemoryReadScope(
            workspace_id=str(root.resolve()),
            share_group_id="group-a",
            agent_instance_id="agent-a",
            project_ref=str(root.resolve()),
            provider="gui",
            runtime_role="gui",
            admin=True,
        ),
        memory_id=memory_id,
    )


def _gui_api(root: Path) -> GovernanceApi:
    return GovernanceApi(
        str(root),
        _trusted_access_context=AccessContext(
            trusted_agent_id="agent-a",
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="governance-engine-gui",
            session_source="transport",
            session_trusted=True,
        ),
    )


def test_engine_result_contract_locked_guard_and_idempotency(tmp_path):
    _activate_v2_workspace(tmp_path)
    GroupControlService(tmp_path, write=True).bind_agent("agent-a", "group-a")
    context = _native_context(tmp_path)
    port = NativeV2RuntimePort(
        tmp_path,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 1},
    )
    write = port.dispatch_mcp(
        "memoryguard_memory_write",
        {
            "memory_id": "memory-a",
            "body": "long-term preference: use pytest -q",
            "kind": "preference",
            "visibility": "ready",
            "evidence": [{"source_ref": "fixture:memory-a", "authority": "test"}],
            "idempotency_key": "seed-memory-a",
        },
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert write["ok"] is True, write

    first = port.dispatch_mcp(
        "memoryguard_memory_update",
        {
            "memory_id": "memory-a",
            "body": "long-term preference: use pytest -q",
            "idempotency_key": "retry-1",
        },
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert first["ok"] is True, first
    assert first["data"]["atom"]["memory_id"] == "memory-a"
    assert first["data"].get("receipt")
    revisions_before_replay = _v2_revisions(tmp_path, "memory-a")
    receipts_before_replay = _v2_receipt_counts(tmp_path)

    replay = port.dispatch_mcp(
        "memoryguard_memory_update",
        {
            "memory_id": "memory-a",
            "body": "long-term preference: use pytest -q",
            "idempotency_key": "retry-1",
        },
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert replay["ok"] is True, replay
    same_receipt = replay["data"].get("receipt") == first["data"].get("receipt")
    assert replay["data"].get("idempotent_replay") is True or same_receipt
    assert replay["data"]["atom"]["memory_id"] == first["data"]["atom"]["memory_id"]
    assert replay["data"]["atom"]["revision"] == first["data"]["atom"]["revision"]
    assert replay["data"]["receipt"]["decision_id"] == first["data"]["receipt"]["decision_id"]
    assert _v2_revisions(tmp_path, "memory-a") == revisions_before_replay
    assert _v2_receipt_counts(tmp_path) == receipts_before_replay

    conflict = port.dispatch_mcp(
        "memoryguard_memory_update",
        {
            "memory_id": "memory-a",
            "body": "different payload",
            "idempotency_key": "retry-1",
        },
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert conflict["ok"] is False
    assert conflict["code"] == "idempotency_conflict"

    scope = {
        "workspace_id": str(tmp_path.resolve()),
        "share_group_id": "group-a",
        "agent_instance_id": "agent-a",
        "project_ref": str(tmp_path.resolve()),
        "provider": "gui",
        "runtime_role": "gui",
    }
    current = MemoryAtomStore(tmp_path).get_atom(
        "memory-a", scope=scope, include_building=True,
    )
    assert current is not None
    mutation_context = V2MutationContext(
        **scope,
        actor="agent-a",
        admin=True,
        authority="manual",
    )
    locked, lock_receipt = GovernanceV2(tmp_path).put_atom(
        replace(current, locked=True),
        context=mutation_context,
        reason="governance engine V2 lock",
        idempotency_key="memory-a-lock",
    )
    assert locked.locked is True
    assert lock_receipt
    unlocked, unlock_receipt = GovernanceV2(tmp_path).put_atom(
        replace(locked, locked=False),
        context=mutation_context,
        reason="governance engine V2 unlock",
        idempotency_key="memory-a-unlock",
    )
    assert unlocked.locked is False
    assert unlock_receipt
    deleted = port.dispatch_mcp(
        "memoryguard_memory_delete",
        {"memory_id": "memory-a", "idempotency_key": "unlocked-delete"},
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert deleted["ok"] is True
    assert deleted["data"]["atom"]["status"] == "deleted"


def test_missing_quarantine_resolution_is_rejected(tmp_path):
    _activate_v2_workspace(tmp_path)
    GroupControlService(tmp_path, write=True).bind_agent("agent-a", "group-a")
    context = _native_context(tmp_path)
    port = NativeV2RuntimePort(
        tmp_path,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 1},
    )

    result = port.dispatch_gui(
        "release_quarantine",
        {
            "operation": "quarantine_release",
            "quarantine_id": "missing",
            "resolution": "typo-delete",
        },
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert result["ok"] is False
    assert result["code"] == "quarantine_not_found"
    listed = port.dispatch_gui(
        "get_quarantine",
        {"operation": "quarantine"},
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert listed["ok"] is True
    assert listed["data"]["total"] == 0


def test_auto_write_idempotency_replays_original_record_without_new_rows(
    tmp_path, monkeypatch,
):
    _activate_v2_workspace(tmp_path)
    GroupControlService(tmp_path, write=True).bind_agent("agent-a", "group-a")
    _configure_v2_identity(monkeypatch, tmp_path)
    arguments = {
        "workspace": str(tmp_path),
        "memory_id": "write-retry-1",
        "body": "V2 idempotent write",
        "kind": "preference",
        "visibility": "ready",
        "evidence_ids": ["evidence-write-retry-1"],
        "idempotency_key": "write-retry-1",
    }
    arguments_before = deepcopy(arguments)
    first = _mcp_payload(execute_tool("memoryguard_memory_write", arguments))
    assert arguments == arguments_before
    assert first["data"]["atom"]["memory_id"] == "write-retry-1"
    revisions_before_replay = _v2_revisions(tmp_path, "write-retry-1")
    receipts_before_replay = _v2_receipt_counts(tmp_path)
    replay = _mcp_payload(execute_tool("memoryguard_memory_write", arguments))
    assert arguments == arguments_before
    same_receipt = replay["data"].get("receipt") == first["data"].get("receipt")
    assert replay["data"].get("idempotent_replay") is True or same_receipt
    assert replay["data"]["atom"]["memory_id"] == first["data"]["atom"]["memory_id"]
    assert replay["data"]["atom"]["revision"] == first["data"]["atom"]["revision"]
    assert _v2_revisions(tmp_path, "write-retry-1") == revisions_before_replay
    assert _v2_receipt_counts(tmp_path) == receipts_before_replay

    conflict = execute_tool(
        "memoryguard_memory_write",
        {**arguments, "body": "different body"},
    )
    assert conflict.get("isError") is True
    assert json.loads(conflict["content"][0]["text"])["code"] == "idempotency_conflict"


def test_gui_and_mcp_mutations_use_public_v2_governance(
    tmp_path, monkeypatch,
):
    _activate_v2_workspace(tmp_path)
    GroupControlService(tmp_path, write=True).bind_agent("agent-a", "group-a")
    _seed_v2_atom(tmp_path)
    api = _gui_api(tmp_path)

    edited = api.edit_memory("mcp-memory", "GUI body", "group-a")
    assert edited["ok"] is True, edited
    locked = api.lock_memory("mcp-memory", "group-a")
    assert locked["ok"] is True, locked

    deleted_gui = api.delete_memory("mcp-memory", "group-a")
    assert deleted_gui["ok"] is True, deleted_gui
    assert "deleted" in json.dumps(deleted_gui).casefold()

    _configure_v2_identity(monkeypatch, tmp_path)
    created = execute_tool(
        "memoryguard_memory_write",
        {
            "workspace": str(tmp_path),
            "memory_id": "mcp-owned",
            "body": "MCP body",
            "kind": "fact",
            "visibility": "ready",
            "evidence_ids": ["evidence-mcp-owned"],
            "idempotency_key": "mcp-write-1",
        },
    )
    assert created.get("isError") is not True, created
    updated = execute_tool(
        "memoryguard_memory_update",
        {
            "workspace": str(tmp_path),
            "memory_id": "mcp-owned",
            "body": "MCP body updated",
            "idempotency_key": "mcp-update-1",
        },
    )
    assert updated.get("isError") is not True, updated
    deleted = execute_tool(
        "memoryguard_memory_delete",
        {
            "workspace": str(tmp_path),
            "memory_id": "mcp-owned",
            "idempotency_key": "mcp-delete-1",
        },
    )
    assert _mcp_payload(deleted)["data"]["atom"]["status"] == "deleted"


def test_auto_organizer_uses_injected_v2_memory_store(tmp_path):
    store = MemoryAtomStore(tmp_path)
    governance = GovernanceV2(tmp_path, memory_store=store)
    organizer = AutoOrganizer(
        tmp_path,
        "group-a",
        store=store,
        engine=governance,
        threshold=0.85,
    )
    record, actions = organizer.organize(MemoryEvent(
        event_id="event",
        agent_instance_id="agent-a",
        share_group_id="group-a",
        raw_content="V2 preference: run pytest",
        metadata={},
    ))
    assert organizer.store is store
    assert record.memory_id
    assert record.share_group_id == "group-a"
    assert isinstance(actions, list)


def test_adapters_do_not_call_store_business_mutations_directly():
    root = Path(__file__).resolve().parents[1]
    forbidden_manual_mutations = (
        "store.edit(", "store.lock(", "store.unlock(", "store.delete(",
        "store.restore(", "store.quarantine_memory(",
        "store.resolve_conflict_group(", "store.close_quarantine(",
        "store._update_record_field(",
    )
    production_adapters = (
        "src/memoryguard/gui.py",
        "src/memoryguard/mcp_server.py",
        "src/memoryguard/external_mcp_detector.py",
        "src/memoryguard/shared_memory_import.py",
    )
    forbidden_write_primitives = (
        ".append_record(", ".supersede(", ".conflict(",
        ".append_decision(", ".update_record(", ".append_event(",
        ".update_event(",
    )
    for relative in production_adapters:
        source = (root / relative).read_text(encoding="utf-8")
        assert "from .auto_organizer import AutoOrganizer" not in source
        assert "AutoOrganizer(" not in source
        for token in (*forbidden_manual_mutations, *forbidden_write_primitives):
            assert token not in source, f"{relative} bypasses engine via {token}"
