"""Canonical-write regression tests across memory kinds and governance safety gates."""

from __future__ import annotations

from pathlib import Path

from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtomStore
from memoryguard.rule_definition import build_definition
from memoryguard.rule_merge import RuleMergeService, RuleMergeStore
from memoryguard.runtime_v2.organizer import V2MemoryOrganizer


GROUP = "canonical-write-group"


def _organizer(workspace: Path) -> V2MemoryOrganizer:
    memory = MemoryAtomStore(workspace)
    return V2MemoryOrganizer(
        workspace,
        GROUP,
        memory_store=memory,
        governance=GovernanceV2(workspace, memory_store=memory),
    )


def _context(workspace: Path, *, project: str = "project-a", runtime: str = "runtime-a") -> V2MutationContext:
    return V2MutationContext(
        workspace_id=str(workspace.resolve()),
        share_group_id=GROUP,
        agent_instance_id="agent-a",
        project_ref=project,
        provider="provider-a",
        runtime_role=runtime,
        actor="test",
        admin=True,
        authority="system",
    )


def _write(
    organizer: V2MemoryOrganizer,
    workspace: Path,
    body: str,
    *,
    kind: str,
    event_id: str,
    injection_policy: str = "relevant",
    project: str = "project-a",
    runtime: str = "runtime-a",
    metadata: dict | None = None,
    idempotency_key: str = "",
) -> dict:
    return organizer.write(
        {
            "body": body,
            "kind": kind,
            "event_id": event_id,
            "agent_instance_id": "agent-a",
            "share_group_id": GROUP,
            "project_ref": project,
            "provider": "provider-a",
            "runtime_role": runtime,
            "visibility": "active",
            "injection_policy": injection_policy,
            "metadata": metadata or {},
            "idempotency_key": idempotency_key,
        },
        context=_context(workspace, project=project, runtime=runtime),
    )


def _atoms(organizer: V2MemoryOrganizer) -> list:
    return organizer.store.list_atoms(scope=organizer.scope, include_building=True)


def test_fact_preference_procedure_synonyms_share_one_canonical_atom(tmp_path: Path) -> None:
    organizer = _organizer(tmp_path)
    first = _write(
        organizer,
        tmp_path,
        "Always use rtk for shell commands",
        kind="fact",
        event_id="event-fact",
        injection_policy="always",
    )
    second = _write(
        organizer,
        tmp_path,
        "Use RTK for shell commands by default",
        kind="preference",
        event_id="event-preference",
    )
    third = _write(
        organizer,
        tmp_path,
        "The procedure is to use RTK for shell commands",
        kind="procedure",
        event_id="event-procedure",
    )

    assert second["mutation_kind"] == "deduplicated"
    assert third["mutation_kind"] == "deduplicated"
    assert len(_atoms(organizer)) == 1
    assert {first["memory_id"], second["memory_id"], third["memory_id"]} == {first["memory_id"]}
    assert {item.get("kind") for item in _atoms(organizer)[0].provenance} >= {
        "fact", "preference", "procedure",
    }
    assert _atoms(organizer)[0].metadata["canonical_policy_reconciled"]["canonical"] == "always"


def test_repeated_kind_does_not_downgrade_explicit_always(tmp_path: Path) -> None:
    organizer = _organizer(tmp_path)
    first = _write(
        organizer,
        tmp_path,
        "Always use rtk for shell commands",
        kind="procedure",
        event_id="event-always",
        injection_policy="always",
    )
    second = _write(
        organizer,
        tmp_path,
        "Use RTK for shell commands by default",
        kind="fact",
        event_id="event-relevant",
        injection_policy="relevant",
    )

    atom = _atoms(organizer)[0]
    assert second["mutation_kind"] == "deduplicated"
    assert second["memory_id"] == first["memory_id"]
    assert atom.injection_policy == "always"


def test_additive_update_creates_traceable_version_without_policy_downgrade(tmp_path: Path) -> None:
    organizer = _organizer(tmp_path)
    first = _write(
        organizer,
        tmp_path,
        "Always use rtk for shell commands",
        kind="procedure",
        event_id="event-base",
        injection_policy="always",
    )
    second = _write(
        organizer,
        tmp_path,
        "Use rtk for shell commands before commit",
        kind="fact",
        event_id="event-additive",
        injection_policy="relevant",
    )

    atoms = _atoms(organizer)
    old = next(atom for atom in atoms if atom.memory_id == first["memory_id"])
    current = next(atom for atom in atoms if atom.memory_id == second["memory_id"])
    assert second["mutation_kind"] == "superseded"
    assert old.status == "superseded"
    assert current.status == "active"
    assert current.injection_policy == "always"
    assert current.supersedes == [old.memory_id]


def test_polarity_conflict_is_not_silently_merged(tmp_path: Path) -> None:
    organizer = _organizer(tmp_path)
    _write(
        organizer,
        tmp_path,
        "Always use rtk for shell commands",
        kind="procedure",
        event_id="event-positive",
    )
    result = _write(
        organizer,
        tmp_path,
        "Never use rtk for shell commands",
        kind="preference",
        event_id="event-negative",
    )

    assert result["mutation_kind"] == "conflicted"
    assert len(_atoms(organizer)) == 2


def test_different_governance_scope_is_not_merged(tmp_path: Path) -> None:
    organizer = _organizer(tmp_path)
    first = _write(
        organizer,
        tmp_path,
        "Always use rtk for shell commands",
        kind="fact",
        event_id="event-project-a",
        project="project-a",
    )
    second = _write(
        organizer,
        tmp_path,
        "Use RTK for shell commands by default",
        kind="preference",
        event_id="event-project-b",
        project="project-b",
    )

    assert second["mutation_kind"] == "created"
    assert second["memory_id"] != first["memory_id"]
    assert len(_atoms(organizer)) == 2


def test_repeated_idempotent_write_does_not_add_another_atom(tmp_path: Path) -> None:
    organizer = _organizer(tmp_path)
    payload = {
        "body": "Always use rtk for shell commands",
        "kind": "procedure",
        "event_id": "event-stable",
        "idempotency_key": "stable-write",
        "agent_instance_id": "agent-a",
        "share_group_id": GROUP,
        "project_ref": "project-a",
        "provider": "provider-a",
        "runtime_role": "runtime-a",
        "visibility": "active",
    }
    first = organizer.write(payload, context=_context(tmp_path))
    second = organizer.write(payload, context=_context(tmp_path))

    assert first["memory_id"] == second["memory_id"]
    assert second["idempotent_replay"] is True
    assert len(_atoms(organizer)) == 1


def test_legacy_rule_merge_recall_crosses_kind_without_relaxing_safety_gates(tmp_path: Path) -> None:
    store = RuleMergeStore(tmp_path)
    left = build_definition("Always use rtk for shell commands", kind="fact")
    right = build_definition("Always use rtk for shell commands", kind="procedure")

    pairs = RuleMergeService(store)._candidate_pairs([left, right])

    assert len(pairs) == 1
    assert {item.rule_kind for item in pairs[0]} == {"fact", "procedure"}
