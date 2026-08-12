from __future__ import annotations

from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.evidence.store import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory.store import MemoryAtom, MemoryAtomStore
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


class _Manifest:
    def __init__(self, state: str = "V2_ACTIVE", generation: int = 7):
        self.state = state
        self.generation = generation

    def current(self):
        return {"state": self.state, "generation": self.generation}


def _context(workspace: Path, *, admin: bool = True, agent: str = "agent-a", group: str = "group-a"):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id="gui-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace),
        share_group_id=group,
        project_ref="project-a",
        provider="codex",
        runtime_role="root",
        entrypoint="gui",
    )


def _seed_memory(workspace: Path) -> tuple[MemoryAtomStore, EvidenceStore]:
    memory = MemoryAtomStore(workspace)
    evidence = EvidenceStore(workspace)
    governance = GovernanceV2(workspace, memory_store=memory, evidence_store=evidence)
    scope = {
        "workspace_id": str(workspace),
        "share_group_id": "group-a",
        "agent_instance_id": "agent-a",
        "project_ref": "project-a",
        "provider": "codex",
        "runtime_role": "root",
        "actor": "seed",
        "authority": "manual",
    }
    atom = MemoryAtom(
        memory_id="memory-a",
        body="original body",
        kind="preference",
        status="active",
        confidence=0.83,
        locked=False,
        injection_policy="relevant",
        priority=7,
        metadata={"keep": "yes", "nested": {"value": 3}},
        workspace_id=str(workspace),
        share_group_id="group-a",
        agent_instance_id="agent-a",
        project_ref="project-a",
        provider="codex",
        runtime_role="root",
    )
    persisted, _ = governance.put_atom(
        atom,
        context=scope,
        evidence=[{"source_ref": "seed:memory-a", "authority": "governance"}],
        reason="seed",
        confidence=1.0,
        idempotency_key="seed-memory-a",
    )
    memory.project_evidence(evidence)
    memory.set_visibility("active", atom_ids=[persisted.atom_id])
    return memory, evidence


def _read(memory: MemoryAtomStore):
    return memory.get_atom(
        "memory-a",
        scope={
            "workspace_id": str(memory.layout.workspace),
            "share_group_id": "group-a",
            "agent_instance_id": "agent-a",
            "project_ref": "project-a",
            "provider": "codex",
            "runtime_role": "root",
        },
        include_building=True,
    )


def test_gui_partial_memory_mutations_preserve_complete_v2_atom_state(tmp_path: Path):
    memory, _evidence = _seed_memory(tmp_path)
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    context = _context(tmp_path)

    locked = port.dispatch_gui(
        "lock_memory", {"memory_id": "memory-a"},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert locked["ok"] is True, locked
    atom = _read(memory)
    assert atom is not None
    assert atom.locked is True
    assert atom.kind == "preference"
    assert atom.confidence == 0.83
    assert atom.injection_policy == "relevant"
    assert atom.priority == 7
    assert atom.metadata == {"keep": "yes", "nested": {"value": 3}}

    policy = port.dispatch_gui(
        "set_memory_injection_policy",
        {"memory_id": "memory-a", "injection_policy": "always", "priority": 33},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert policy["ok"] is True, policy
    atom = _read(memory)
    assert atom is not None
    assert atom.locked is True
    assert atom.kind == "preference"
    assert atom.confidence == 0.83
    assert atom.injection_policy == "always"
    assert atom.priority == 33
    assert atom.metadata == {"keep": "yes", "nested": {"value": 3}}

    edited = port.dispatch_gui(
        "edit_memory",
        {"memory_id": "memory-a", "body": "updated body"},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert edited["ok"] is True, edited
    atom = _read(memory)
    assert atom is not None
    assert atom.body == "updated body"
    assert atom.locked is True
    assert atom.kind == "preference"
    assert atom.confidence == 0.83
    assert atom.injection_policy == "always"
    assert atom.priority == 33
    assert atom.metadata == {"keep": "yes", "nested": {"value": 3}}

    deleted = port.dispatch_gui(
        "delete_memory", {"memory_id": "memory-a"},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert deleted["ok"] is True, deleted
    assert _read(memory).status == "deleted"

    restored = port.dispatch_gui(
        "restore_memory", {"memory_id": "memory-a"},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert restored["ok"] is True, restored
    atom = _read(memory)
    assert atom.status == "active"
    assert atom.body == "updated body"
    assert atom.kind == "preference"
    assert atom.confidence == 0.83
    assert atom.locked is True
    assert atom.injection_policy == "always"
    assert atom.priority == 33


def test_gui_memory_rollback_replays_scoped_v2_revision_with_governance_receipt(tmp_path: Path):
    memory, _evidence = _seed_memory(tmp_path)
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    context = _context(tmp_path)
    entry = next(
        item for item in port.coverage()["surfaces"]["gui"]["entries"]
        if item["name"] == "rollback_memory"
    )
    assert entry["status"] == "implemented"
    assert entry["reason"] == ""

    edited = port.dispatch_gui(
        "edit_memory", {"memory_id": "memory-a", "body": "changed before rollback"},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert edited["ok"] is True, edited
    assert _read(memory).body == "changed before rollback"

    revisions = memory.list_revisions(
        scope={
            "workspace_id": str(tmp_path),
            "share_group_id": "group-a",
            "agent_instance_id": "agent-a",
            "project_ref": "project-a",
            "provider": "codex",
            "runtime_role": "root",
        },
        memory_id="memory-a",
    )
    original = next(item for item in revisions if item["revision"] == 1)
    result = port.dispatch_gui(
        "rollback_memory", {"version_id": original["version_id"]},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    assert result["data"]["version_id"] == original["version_id"]
    assert result["data"]["receipt"]["operation"] == "put"
    restored = _read(memory)
    assert restored is not None
    assert restored.body == "original body"
    assert restored.kind == "preference"
    assert restored.confidence == 0.83
    assert restored.metadata == {"keep": "yes", "nested": {"value": 3}}


def test_gui_rule_audience_update_uses_v2_bindings_and_trusted_scope(tmp_path: Path):
    RuleV2Store(tmp_path)
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    context = _context(tmp_path, admin=True)

    created = port.dispatch_gui(
        "create_rule_from_text",
        {"text": "Always run focused tests before the full suite"},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert created["ok"] is True, created
    definition_id = created["data"]["definition_id"]

    updated = port.dispatch_gui(
        "update_rule_audience",
        {
            "memory_id": definition_id,
            "injection_policy": "always",
            "priority": 40,
            "assignments": [{
                "target_type": "agent_project",
                "target_id": "agent-a",
                "project_ref": "project-a",
                "effect": "include",
                "priority": 40,
            }],
        },
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert updated["ok"] is True, updated
    assert updated["data"]["definition_id"] == definition_id
    assert updated["data"]["bindings"][0]["target_type"] == "agent_project"

    snapshot = port.dispatch_gui(
        "preview_effective_rules", {},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert snapshot["ok"] is True, snapshot
    assert any(item["definition_id"] == definition_id for item in snapshot["data"]["effective"])

    denied = port.dispatch_gui(
        "update_rule_audience",
        {
            "memory_id": definition_id,
            "injection_policy": "always",
            "assignments": [{"target_type": "agent", "target_id": "other-agent", "effect": "include"}],
        },
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert denied["ok"] is False
    assert denied["code"] == "unknown_agent_target"
