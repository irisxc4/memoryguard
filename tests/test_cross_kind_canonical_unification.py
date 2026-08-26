"""Cross-kind canonical unification regressions."""

from __future__ import annotations

from itertools import permutations
from pathlib import Path

from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtomStore
from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rule_reconciliation import reconcile_historical_duplicates
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.organizer import V2MemoryOrganizer


GROUP = "cross-kind-canonical"


def _context(workspace: Path) -> V2MutationContext:
    return V2MutationContext(
        workspace_id=str(workspace.resolve()),
        share_group_id=GROUP,
        agent_instance_id="agent-a",
        project_ref="project-a",
        provider="provider-a",
        runtime_role="runtime-a",
        actor="cross-kind-test",
        admin=True,
        authority="system",
    )


def _run(workspace: Path, records: tuple[tuple[str, str, str], ...]):
    memory = MemoryAtomStore(workspace)
    organizer = V2MemoryOrganizer(
        workspace,
        GROUP,
        memory_store=memory,
        governance=GovernanceV2(workspace, memory_store=memory),
    )
    results = []
    for body, kind, event_id in records:
        results.append(
            organizer.write(
                {
                    "body": body,
                    "kind": kind,
                    "event_id": event_id,
                    "agent_instance_id": "agent-a",
                    "share_group_id": GROUP,
                    "project_ref": "project-a",
                    "provider": "provider-a",
                    "runtime_role": "runtime-a",
                    "visibility": "active",
                    "injection_policy": "always" if kind == "fact" else "relevant",
                },
                context=_context(workspace),
            )
        )
    atoms = memory.list_atoms(scope=organizer.scope, include_building=True)
    return results, atoms


def test_cross_kind_canonical_kind_and_policy_are_order_independent(tmp_path: Path) -> None:
    records = (
        ("Always use rtk for shell commands", "fact", "event-fact"),
        ("Use RTK for shell commands by default", "preference", "event-preference"),
        ("The procedure is to use RTK for shell commands", "procedure", "event-procedure"),
    )

    observed = []
    for index, order in enumerate(permutations(records)):
        results, atoms = _run(tmp_path / str(index), order)
        active = [atom for atom in atoms if atom.status == "active"]
        assert len(active) == 1
        assert len(atoms) == 1
        assert {item["action"] for result in results[1:] for item in result["actions"]} & {
            "merge_provenance"
        }
        observed.append((active[0].kind, active[0].injection_policy))
        assert {item.get("kind") for item in active[0].provenance} == {
            "fact", "preference", "procedure",
        }

    assert set(observed) == {("procedure", "always")}


def test_explicit_classification_override_is_preserved_in_either_order(tmp_path: Path) -> None:
    records = (
        ("Always use rtk for shell commands", "procedure", "event-procedure"),
        ("Use RTK for shell commands by default", "fact", "event-fact"),
    )
    observed = []
    for index, order in enumerate((records, tuple(reversed(records)))):
        workspace = tmp_path / f"override-{index}"
        memory = MemoryAtomStore(workspace)
        organizer = V2MemoryOrganizer(
            workspace,
            GROUP,
            memory_store=memory,
            governance=GovernanceV2(workspace, memory_store=memory),
        )
        for body, kind, event_id in order:
            organizer.write(
                {
                    "body": body,
                    "kind": kind,
                    "event_id": event_id,
                    "agent_instance_id": "agent-a",
                    "share_group_id": GROUP,
                    "project_ref": "project-a",
                    "provider": "provider-a",
                    "runtime_role": "runtime-a",
                    "visibility": "active",
                    "metadata": {
                        "classification_override": kind == "fact",
                    },
                },
                context=_context(workspace),
            )
        active = [
            atom for atom in memory.list_atoms(
                scope=organizer.scope, include_building=True,
            ) if atom.status == "active"
        ]
        assert len(active) == 1
        observed.append(active[0].kind)
    assert observed == ["fact", "fact"]


def _seed_historical_rule(
    store: RuleV2Store,
    *,
    kind: str,
    definition_id: str,
    source_id: str,
) -> None:
    definition = build_definition(
        "Always use rtk for shell commands",
        kind=kind,
        rule_strength="must",
    )
    definition = definition.__class__(
        **{**definition.to_dict(), "definition_id": definition_id}
    )
    store.upsert_definition(definition)
    store.upsert_binding(build_binding(
        definition_id,
        share_group_id=GROUP,
        target_type="group",
        target_id=GROUP,
        binding_id=f"binding-{definition_id}",
    ))
    store.upsert_source_link(
        source_kind="native",
        share_group_id=GROUP,
        memory_id=source_id,
        source_ref=source_id,
        original_definition_id=definition_id,
        canonical_definition_id=definition_id,
        status="active",
    )


def test_historical_kind_priority_is_independent_of_insert_and_definition_id_order(
    tmp_path: Path,
) -> None:
    records = (
        ("fact", "aaa-fact", "source-fact"),
        ("preference", "middle-preference", "source-preference"),
        ("procedure", "zzz-procedure", "source-procedure"),
    )
    for index, order in enumerate(permutations(records)):
        workspace = tmp_path / f"history-{index}"
        store = RuleV2Store(workspace)
        for kind, definition_id, source_id in order:
            _seed_historical_rule(
                store,
                kind=kind,
                definition_id=definition_id,
                source_id=source_id,
            )
        first = reconcile_historical_duplicates(workspace, GROUP, store=store)
        assert first["merged_count"] == 2
        active = store.list_definitions(status="active")
        assert len(active) == 1
        assert active[0].definition_id == "zzz-procedure"
        assert active[0].rule_kind == "procedure"
        assert {
            link["canonical_definition_id"]
            for link in store.list_source_links(
                share_group_id=GROUP, status="active",
            )
        } == {"zzz-procedure"}
        before_second = store.metrics()
        second = reconcile_historical_duplicates(workspace, GROUP, store=store)
        assert second["merged_count"] == 0
        assert store.metrics() == before_second
