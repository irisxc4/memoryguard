"""Focused regressions for reversible historical claim composition."""
from __future__ import annotations

from pathlib import Path

from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rule_reconciliation import reconcile_historical_duplicates
from memoryguard.rules.v2_store import RuleV2Store


GROUP = "shared-9b8b5d020a74b2fd"


def _seed(
    store: RuleV2Store,
    body: str,
    *,
    kind: str,
    definition_id: str,
    source_id: str,
    strength: str = "must",
    priority: int = 0,
    group: str = GROUP,
    source_kind: str = "native",
    target_type: str = "group",
    target_id: str = "",
    project_ref: str = "",
) -> object:
    definition = build_definition(
        body, kind=kind, rule_strength=strength,
    )
    definition = definition.__class__(
        **{**definition.to_dict(), "definition_id": definition_id},
    )
    store.upsert_definition(definition)
    store.upsert_binding(build_binding(
        definition_id,
        share_group_id=group,
        target_type=target_type,
        target_id=target_id or group,
        project_ref=project_ref,
        priority=priority,
        binding_id=f"binding-{source_id}",
    ))
    store.upsert_source_link(
        source_kind=source_kind,
        share_group_id=group,
        memory_id=source_id,
        source_ref=source_id,
        source_revision="1",
        original_definition_id=definition_id,
        canonical_definition_id=definition_id,
        status="active",
    )
    # Preserve the source surface used by the claim composer.  The definition
    # projection remains normalized, while ``text`` is the historical body.
    store._write(lambda conn: conn.execute(
        "UPDATE rule_definitions SET text=? WHERE definition_id=?",
        (body, definition_id),
    ))
    return definition


def test_historical_reconcile_composes_same_scope_claims_and_dedupes(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    procedure = _seed(
        store,
        "Always preserve topic records",
        kind="procedure",
        definition_id="claim-procedure",
        source_id="source-procedure",
    )
    preference = _seed(
        store,
        "Preserve topic records in CI exports",
        kind="preference",
        definition_id="claim-preference",
        source_id="source-preference",
    )
    fact = _seed(
        store,
        "Preserve topic records when review starts\nPreserve topic records",
        kind="fact",
        definition_id="claim-fact",
        source_id="source-fact",
    )

    result = reconcile_historical_duplicates(tmp_path, GROUP, store=store)

    assert result["merged_count"] == 2
    assert {item.status for item in store.list_definitions(status="active")} == {"active"}
    winner_id = next(
        item.definition_id for item in store.list_definitions(status="active")
    )
    assert winner_id == procedure.definition_id
    winner = store.get_definition(winner_id)
    assert winner is not None
    body = store._read(lambda conn: conn.execute(
        "SELECT text FROM rule_definitions WHERE definition_id=?",
        (winner_id,),
    ).fetchone()[0])
    assert "ci" in body.casefold()
    assert "review starts" in body.casefold()
    # The base sentence from the fact is an equivalent paraphrase of the
    # review claim and is rendered once; CI and review remain additive.
    assert len([line for line in body.splitlines() if line.strip()]) == 2
    assert body.casefold().count("- preserve topic records\n") <= 1
    assert {
        link["canonical_definition_id"]
        for link in store.list_source_links(
            share_group_id=GROUP, status="active",
        )
    } == {winner_id}
    assert store.get_definition(preference.definition_id).status == "alias"
    assert store.get_definition(fact.definition_id).status == "alias"


def test_same_audience_different_source_kinds_merge_exact_body(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    native = _seed(
        store,
        "Always process topic records",
        kind="procedure",
        definition_id="source-kind-native",
        source_id="source-native",
        source_kind="native",
    )
    imported = _seed(
        store,
        "Always process topic records",
        kind="fact",
        definition_id="source-kind-imported",
        source_id="source-imported",
        source_kind="shared_memory",
    )

    result = reconcile_historical_duplicates(tmp_path, GROUP, store=store)

    assert result["merged_count"] == 1
    assert store.get_definition(imported.definition_id).status == "alias"
    links = store.list_source_links(share_group_id=GROUP, status="active")
    assert {link["source_kind"] for link in links} == {"native", "shared_memory"}
    assert {link["canonical_definition_id"] for link in links} == {
        native.definition_id,
    }


def test_different_audiences_exact_body_merge_with_all_bindings(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    first = _seed(
        store,
        "Always process topic records",
        kind="procedure",
        definition_id="audience-exact-a",
        source_id="source-audience-exact-a",
        target_id="audience-a",
    )
    second = _seed(
        store,
        "Always process topic records",
        kind="fact",
        definition_id="audience-exact-b",
        source_id="source-audience-exact-b",
        target_id="audience-b",
    )

    result = reconcile_historical_duplicates(tmp_path, GROUP, store=store)

    assert result["merged_count"] == 1
    assert store.get_definition(second.definition_id).status == "alias"
    bindings = store.list_bindings(
        definition_id=first.definition_id, status="active",
    )
    assert {binding.target_id for binding in bindings} == {
        "audience-a", "audience-b",
    }


def test_different_audiences_equivalent_token_order_folds_to_one_canonical(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    first = _seed(
        store,
        "Always run topic tests for export",
        kind="procedure",
        definition_id="audience-equivalent-a",
        source_id="source-audience-equivalent-a",
        target_id="audience-a",
    )
    second = _seed(
        store,
        "Must run export topic tests",
        kind="fact",
        definition_id="audience-equivalent-b",
        source_id="source-audience-equivalent-b",
        target_id="audience-b",
        source_kind="imported",
    )

    result = reconcile_historical_duplicates(tmp_path, GROUP, store=store)

    assert result["merged_count"] == 1
    assert store.get_definition(second.definition_id).status == "alias"
    bindings = store.list_bindings(
        definition_id=first.definition_id, status="active",
    )
    assert {binding.target_id for binding in bindings} == {
        "audience-a", "audience-b",
    }
    links = store.list_source_links(share_group_id=GROUP, status="active")
    assert {link["source_kind"] for link in links} == {"native", "imported"}
    assert {link["canonical_definition_id"] for link in links} == {
        first.definition_id,
    }


def test_same_audience_duplicate_binding_additive_fold_preserves_provenance_and_undo(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    winner = _seed(
        store,
        "Always run topic tests",
        kind="procedure",
        definition_id="duplicate-audience-winner",
        source_id="source-duplicate-winner",
    )
    loser = _seed(
        store,
        "Always run topic tests before export",
        kind="fact",
        definition_id="duplicate-audience-loser",
        source_id="source-duplicate-loser",
        source_kind="imported",
    )
    store.upsert_binding(build_binding(
        loser.definition_id,
        share_group_id=GROUP,
        target_type="group",
        target_id=GROUP,
        binding_id="binding-duplicate-audience",
    ))
    loser_bindings_before = [
        item.to_dict() for item in store.list_bindings(
            definition_id=loser.definition_id, status="active",
        )
    ]

    result = reconcile_historical_duplicates(tmp_path, GROUP, store=store)

    assert result["merged_count"] == 1
    assert store.get_definition(loser.definition_id).status == "alias"
    assert store._read(lambda conn: conn.execute(
        "SELECT COUNT(*) FROM rule_bindings WHERE definition_id=?",
        (loser.definition_id,),
    ).fetchone()[0]) == 2
    links = store.list_source_links(share_group_id=GROUP, status="active")
    assert {link["source_kind"] for link in links} == {"native", "imported"}
    assert {link["canonical_definition_id"] for link in links} == {
        winner.definition_id,
    }

    decision_id = result["details"][0]["decision_id"]
    assert store.undo_historical_duplicate(
        decision_id, share_group_id=GROUP,
    )["status"] == "undone"
    assert [
        item.to_dict() for item in store.list_bindings(
            definition_id=loser.definition_id, status="active",
        )
    ] == loser_bindings_before
    links = store.list_source_links(share_group_id=GROUP, status="active")
    assert {
        link["source_ref"]: link["canonical_definition_id"]
        for link in links
    } == {
        "source-duplicate-winner": winner.definition_id,
        "source-duplicate-loser": loser.definition_id,
    }


def test_different_audiences_additive_and_project_content_stay_separate(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    base = _seed(
        store,
        "Always run topic tests",
        kind="procedure",
        definition_id="audience-additive-base",
        source_id="source-audience-additive-base",
        target_id="audience-a",
    )
    additive = _seed(
        store,
        "Always run topic tests before export",
        kind="fact",
        definition_id="audience-additive-extra",
        source_id="source-audience-additive-extra",
        target_id="audience-b",
    )
    project_a = _seed(
        store,
        "Always process topic records for project data",
        kind="procedure",
        definition_id="project-a",
        source_id="source-project-a",
        target_type="project",
        target_id="project-a",
        project_ref="project-a",
    )
    project_b = _seed(
        store,
        "Process topic records for project data during export",
        kind="fact",
        definition_id="project-b",
        source_id="source-project-b",
        target_type="project",
        target_id="project-b",
        project_ref="project-b",
    )

    result = reconcile_historical_duplicates(tmp_path, GROUP, store=store)

    assert result["merged_count"] == 0
    assert {
        item.definition_id for item in store.list_definitions(status="active")
    } == {
        base.definition_id, additive.definition_id,
        project_a.definition_id, project_b.definition_id,
    }


def test_different_audience_exact_fold_undo_restores_both_sides(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    first = _seed(
        store,
        "Always process topic records",
        kind="procedure",
        definition_id="undo-audience-a",
        source_id="source-undo-audience-a",
        target_id="audience-a",
    )
    second = _seed(
        store,
        "Must process topic records",
        kind="fact",
        definition_id="undo-audience-b",
        source_id="source-undo-audience-b",
        target_id="audience-b",
    )
    before_first = store.get_definition(first.definition_id).to_dict()
    before_second = store.get_definition(second.definition_id).to_dict()

    result = reconcile_historical_duplicates(tmp_path, GROUP, store=store)

    assert result["merged_count"] == 1
    decision_id = result["details"][0]["decision_id"]
    assert store.undo_historical_duplicate(
        decision_id, share_group_id=GROUP,
    )["status"] == "undone"
    assert store.get_definition(first.definition_id).to_dict() == before_first
    assert store.get_definition(second.definition_id).to_dict() == before_second
    assert {
        binding.target_id for binding in store.list_bindings(status="active")
        if binding.definition_id in {first.definition_id, second.definition_id}
    } == {"audience-a", "audience-b"}
    links = store.list_source_links(share_group_id=GROUP, status="active")
    assert {
        link["canonical_definition_id"] for link in links
    } == {first.definition_id, second.definition_id}


def test_conflict_and_unrelated_historical_claims_stay_independent(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    base = _seed(
        store,
        "Always preserve topic records",
        kind="procedure",
        definition_id="independent-base",
        source_id="source-independent-base",
    )
    conflict = _seed(
        store,
        "Never preserve topic records",
        kind="preference",
        definition_id="independent-conflict",
        source_id="source-independent-conflict",
    )
    unrelated = _seed(
        store,
        "Keep encrypted archives for releases",
        kind="fact",
        definition_id="independent-unrelated",
        source_id="source-independent-unrelated",
    )

    result = reconcile_historical_duplicates(tmp_path, GROUP, store=store)

    assert result["merged_count"] == 0
    assert {item.definition_id for item in store.list_definitions(status="active")} == {
        base.definition_id, conflict.definition_id, unrelated.definition_id,
    }


def test_topic_fold_ignores_noisy_governance_projections_and_keeps_strongest_metadata(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    must = _seed(
        store,
        "Must process topic records",
        kind="fact",
        definition_id="topic-must",
        source_id="source-topic-must",
        strength="must",
        priority=3,
    )
    safety = _seed(
        store,
        "Do not delete topic records",
        kind="procedure",
        definition_id="topic-safety",
        source_id="source-topic-safety",
        strength="observation",
        priority=9,
    )
    # Historical classifiers can disagree with the body.  These projections
    # must not split an otherwise safe topic fold.
    store._write(lambda conn: conn.execute(
        "UPDATE rule_definitions SET polarity='positive',"
        "normalized_intent=? WHERE definition_id=?",
        ('{"action":"noise","object":"noise"}', safety.definition_id),
    ))

    result = reconcile_historical_duplicates(tmp_path, GROUP, store=store)

    assert result["merged_count"] == 1
    active = store.list_definitions(status="active")
    assert len(active) == 1
    canonical = active[0]
    assert canonical.rule_strength == "must"
    assert canonical.rule_kind == "procedure"
    body = store._read(lambda conn: conn.execute(
        "SELECT text FROM rule_definitions WHERE definition_id=?",
        (canonical.definition_id,),
    ).fetchone()[0])
    assert "process topic records" in body.casefold()
    assert "do not delete topic records" in body.casefold()
    binding = store.list_bindings(
        definition_id=canonical.definition_id, status="active",
    )[0]
    assert binding.priority == 9


def test_equal_body_cross_layer_historical_claims_do_not_collapse_in_either_order(
    tmp_path: Path,
) -> None:
    body = "shared release process"
    for first_strength, second_strength in (
        ("must", "observation"),
        ("observation", "must"),
    ):
        workspace = tmp_path / f"{first_strength}-first"
        store = RuleV2Store(workspace)
        first = _seed(
            store,
            body,
            kind="procedure",
            definition_id="equal-first",
            source_id="source-equal-first",
            strength=first_strength,
        )
        second = _seed(
            store,
            body,
            kind="procedure",
            definition_id="equal-second",
            source_id="source-equal-second",
            strength=second_strength,
        )

        result = reconcile_historical_duplicates(workspace, GROUP, store=store)

        assert result["merged_count"] == 0
        active = store.list_definitions(status="active")
        assert {item.definition_id for item in active} == {
            first.definition_id, second.definition_id,
        }
        assert {item.rule_strength for item in active} == {"must", "observation"}


def test_topic_fold_requires_exact_scope_and_rejects_direct_conflict(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    same_topic = _seed(
        store,
        "Always run topic tests",
        kind="procedure",
        definition_id="topic-scope-a",
        source_id="source-topic-scope-a",
    )
    different_scope = _seed(
        store,
        "Always run topic tests before export",
        kind="fact",
        definition_id="topic-scope-b",
        source_id="source-topic-scope-b",
    )
    store.upsert_binding(build_binding(
        different_scope.definition_id,
        share_group_id=GROUP,
        target_type="group",
        target_id="another-scope",
        binding_id="binding-topic-scope-extra",
    ))
    conflict = _seed(
        store,
        "Never run topic tests",
        kind="preference",
        definition_id="topic-conflict",
        source_id="source-topic-conflict",
    )

    result = reconcile_historical_duplicates(tmp_path, GROUP, store=store)

    assert result["merged_count"] == 0
    assert {item.definition_id for item in store.list_definitions(status="active")} == {
        same_topic.definition_id,
        different_scope.definition_id,
        conflict.definition_id,
    }
    assert any(
        detail["reason"] == "scope_mismatch"
        for detail in result["details"]
        if detail["status"] == "preserved_conflict"
    )


def test_composed_fold_undo_restores_winner_body_and_is_idempotent(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    winner = _seed(
        store,
        "Always preserve topic records",
        kind="procedure",
        definition_id="undo-composed-winner",
        source_id="source-undo-composed-winner",
    )
    loser = _seed(
        store,
        "Preserve topic records in CI exports",
        kind="preference",
        definition_id="undo-composed-loser",
        source_id="source-undo-composed-loser",
        priority=9,
    )
    winner_before = store.get_definition(winner.definition_id).to_dict()
    loser_before = store.get_definition(loser.definition_id).to_dict()
    winner_binding_before = [
        item.to_dict() for item in store.list_bindings(
            definition_id=winner.definition_id, status="active",
        )
    ]
    loser_binding_before = [
        item.to_dict() for item in store.list_bindings(
            definition_id=loser.definition_id, status="active",
        )
    ]

    first = reconcile_historical_duplicates(tmp_path, GROUP, store=store)
    decision_id = first["details"][0]["decision_id"]
    composed_body = store._read(lambda conn: conn.execute(
        "SELECT text FROM rule_definitions WHERE definition_id=?",
        (winner.definition_id,),
    ).fetchone()[0])
    replay = reconcile_historical_duplicates(tmp_path, GROUP, store=store)
    assert replay["merged_count"] == 0
    assert store._read(lambda conn: conn.execute(
        "SELECT text FROM rule_definitions WHERE definition_id=?",
        (winner.definition_id,),
    ).fetchone()[0]) == composed_body

    undone = store.undo_historical_duplicate(decision_id, share_group_id=GROUP)
    assert undone["status"] == "undone"
    assert store.get_definition(winner.definition_id).to_dict() == winner_before
    assert store.get_definition(loser.definition_id).to_dict() == loser_before
    assert [
        item.to_dict() for item in store.list_bindings(
            definition_id=winner.definition_id, status="active",
        )
    ] == winner_binding_before
    assert [
        item.to_dict() for item in store.list_bindings(
            definition_id=loser.definition_id, status="active",
        )
    ] == loser_binding_before
    assert store.undo_historical_duplicate(
        decision_id, share_group_id=GROUP,
    ).get("already_undone") is True
