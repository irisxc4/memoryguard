from __future__ import annotations

from pathlib import Path

from memoryguard.memory.store import MemoryAtom, MemoryAtomStore, MemoryReadScope
from memoryguard.runtime_v2.context_engine import ContextEngine


def test_native_audience_optional_dimensions_do_not_narrow_agent_or_group(tmp_path: Path) -> None:
    atom = MemoryAtom(
        memory_id="native-audience",
        atom_id="native-audience",
        body="safe audience marker",
        workspace_id=str(tmp_path),
        agent_instance_id="agent-a",
        share_group_id="group-a",
        metadata={
            "audience": {
                "source": "native_v2",
                "target_type": "agent",
                "target_id": "agent-a",
                # Older native writers persisted these optional dimensions on
                # an agent audience. They are provenance, not hidden filters.
                "project_ref": "project-a",
                "provider": "codex",
                "runtime_role": "root",
                "effect": "include",
            }
        },
    )
    empty_dimensions = MemoryReadScope(
        workspace_id=str(tmp_path),
        share_group_id="group-a",
        agent_instance_id="agent-a",
        project_ref="",
        provider="",
        runtime_role="",
    )
    different_optional_dimensions = MemoryReadScope(
        workspace_id=str(tmp_path),
        share_group_id="group-a",
        agent_instance_id="agent-a",
        project_ref="project-b",
        provider="cursor",
        runtime_role="subagent",
    )
    other_agent = MemoryReadScope(
        workspace_id=str(tmp_path),
        share_group_id="group-a",
        agent_instance_id="agent-b",
    )

    assert MemoryAtomStore._atom_visible_to_scope(atom, empty_dimensions) is True
    assert MemoryAtomStore._atom_visible_to_scope(atom, different_optional_dimensions) is True
    assert MemoryAtomStore._atom_visible_to_scope(atom, other_agent) is False


def test_agent_project_audience_remains_project_narrowed(tmp_path: Path) -> None:
    atom = MemoryAtom(
        memory_id="native-agent-project",
        atom_id="native-agent-project",
        body="project-specific audience marker",
        workspace_id=str(tmp_path),
        agent_instance_id="agent-a",
        share_group_id="group-a",
        metadata={
            "audience": {
                "source": "native_v2",
                "target_type": "agent_project",
                "target_id": "agent-a",
                "project_ref": "project-a",
                "effect": "include",
            }
        },
    )
    same_project = MemoryReadScope(
        workspace_id=str(tmp_path),
        share_group_id="group-a",
        agent_instance_id="agent-a",
        project_ref="project-a",
    )
    other_project = MemoryReadScope(
        workspace_id=str(tmp_path),
        share_group_id="group-a",
        agent_instance_id="agent-a",
        project_ref="project-b",
    )

    assert MemoryAtomStore._atom_visible_to_scope(atom, same_project) is True
    assert MemoryAtomStore._atom_visible_to_scope(atom, other_project) is False


def test_old_atom_without_audience_derives_agent_acl_without_optional_narrowing(tmp_path: Path) -> None:
    atom = MemoryAtom(
        memory_id="old-atom",
        atom_id="old-atom",
        body="old persisted atom",
        workspace_id=str(tmp_path),
        agent_instance_id="agent-a",
        share_group_id="group-a",
        project_ref="project-a",
        provider="codex",
        runtime_role="root",
    )
    same_agent_without_optional_scope = MemoryReadScope(
        workspace_id=str(tmp_path), share_group_id="group-a", agent_instance_id="agent-a"
    )
    other_agent = MemoryReadScope(
        workspace_id=str(tmp_path), share_group_id="group-a", agent_instance_id="agent-b"
    )
    anonymous_agent = MemoryReadScope(
        workspace_id=str(tmp_path), share_group_id="group-a", agent_instance_id=""
    )

    assert MemoryAtomStore._atom_visible_to_scope(atom, same_agent_without_optional_scope) is True
    assert MemoryAtomStore._atom_visible_to_scope(atom, other_agent) is False
    assert MemoryAtomStore._atom_visible_to_scope(atom, anonymous_agent) is False


def test_context_engine_receives_safe_retrieval_omission_receipt() -> None:
    engine = ContextEngine(ready=True, state="V2_ACTIVE")
    packet = engine.bootstrap(
        {
            "task": "unscoped knowledge",
            "agent": "agent-a",
            "group": "group-a",
            "project": "project-a",
        },
        {
            "mandatory": [],
            "relevant": [],
            "knowledge": [],
            "reference_only": [],
            "omissions": [{"layer": "knowledge", "reason": "knowledge_scope_required"}],
        },
    ).to_dict()

    assert packet["reference_only"] == []
    assert any(receipt["reason"] == "scope_required" for receipt in packet["receipts"])


def test_history_and_codegraph_adapters_are_exposed_by_bootstrap_module() -> None:
    import memoryguard.context_bootstrap as bootstrap

    assert callable(getattr(bootstrap, "history_reference_candidates", None))
    assert callable(getattr(bootstrap, "codegraph_reference_candidates", None))
