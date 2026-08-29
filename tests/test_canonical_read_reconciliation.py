"""Focused canonical-read/reconciliation regressions.

These tests intentionally use the public stores and read-path helpers.  They
model the old rows that are still present after a partial migration rather
than depending on a live user's data home.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from memoryguard.rule_definition import build_definition
from memoryguard.rule_creation import RuleCreationService
from memoryguard.rule_merge_store import RuleMergeStore
from memoryguard.rule_read_path import (
    MODE_RULE_INTELLIGENCE,
    RuleReadPath,
    dedupe_records,
    resolve_read_path_mode,
)
from memoryguard.rule_reconciliation import (
    canonical_reconciliation_status,
    reconcile_historical_duplicates,
    settle_native_canonical_snapshot,
)
from memoryguard.schema_v3 import MemoryKind, SharedMemoryRecord, SharedMemoryStatus
from memoryguard.rule_binding import build_binding
from memoryguard.rules.v2_store import RuleV2Store


GROUP = "shared-9b8b5d020a74b2fd"


def _merge_store(tmp_path: Path) -> RuleMergeStore:
    return RuleMergeStore(tmp_path)


def _seed_merge_definition(store: RuleMergeStore, text: str, *, kind: str):
    definition = build_definition(
        text,
        kind=kind,
        rule_strength="must",
    )
    return store.upsert_definition(definition)


def test_read_path_accepts_legacy_native_spellings() -> None:
    assert resolve_read_path_mode("native-v2") == MODE_RULE_INTELLIGENCE
    assert resolve_read_path_mode("rule_intelligence") == MODE_RULE_INTELLIGENCE


def test_canonical_status_does_not_report_unknown_for_old_native_value(tmp_path: Path) -> None:
    store = RuleV2Store(tmp_path)
    definition = store.upsert_definition(
        build_definition("use rtk for shell commands", kind="procedure")
    )
    store.upsert_binding(build_binding(
        definition.definition_id,
        share_group_id=GROUP,
        target_type="group",
        target_id=GROUP,
        binding_id="binding-read-path",
    ))
    store.upsert_source_link(
        source_kind="native",
        share_group_id=GROUP,
        memory_id="source-read-path",
        source_ref="source-read-path",
        original_definition_id=definition.definition_id,
        canonical_definition_id=definition.definition_id,
        status="active",
    )
    store.record_canonical_state({
        "scope_id": "old-native-state",
        "share_group_id": GROUP,
        "activation_status": "active",
        "read_path": "legacy",
        "canonical_digest": "",
    })
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE rule_canonical_state SET activation_status='active', read_path='native-v2' "
            "WHERE share_group_id=?",
            (GROUP,),
        )
    result = canonical_reconciliation_status(tmp_path, GROUP, store=store)
    assert "canonical_read_path_unavailable" not in result["failures"]
    assert result["checks"]["read_path"] == MODE_RULE_INTELLIGENCE


def test_dedupe_collapses_alias_sources_to_one_active_head() -> None:
    first = SharedMemoryRecord(
        memory_id="source-a", body="same intent", kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE, injection_policy="always", priority=10,
    )
    second = SharedMemoryRecord(
        memory_id="source-b", body="same intent", kind=MemoryKind.PREFERENCE,
        status=SharedMemoryStatus.ACTIVE, injection_policy="always", priority=1,
    )
    mapping = {
        "memory_to_definition": {"source-a": "head", "source-b": "alias"},
        "definitions": {
            "head": {"status": "active"},
            "alias": {"status": "alias", "superseded_by": "head"},
        },
    }
    assert [item.memory_id for item in dedupe_records([first, second], mapping)] == [
        "source-a"
    ]


def test_historical_duplicate_reconciliation_is_idempotent_and_preserves_links(tmp_path: Path) -> None:
    store = _merge_store(tmp_path)
    first = _seed_merge_definition(store, "Always use rtk for shell commands", kind="fact")
    second = _seed_merge_definition(store, "Always use rtk for shell commands", kind="procedure")
    for definition, source in ((first, "source-fact"), (second, "source-procedure")):
        store.upsert_source_link(
            share_group_id=GROUP,
            memory_id=source,
            original_definition_id=definition.definition_id,
            canonical_definition_id=definition.definition_id,
            status="active",
        )
    before = store.metrics()
    first_run = reconcile_historical_duplicates(tmp_path, GROUP, store=store)
    second_run = reconcile_historical_duplicates(tmp_path, GROUP, store=store)
    assert first_run["merged_count"] == 1
    assert second_run["merged_count"] == 0
    winner = first_run["details"][0]["winner"]
    loser = first_run["details"][0]["merged"]
    assert store.get_definition(loser).status in {"alias", "superseded"}
    assert {
        link["canonical_definition_id"]
        for link in store.list_source_links(share_group_id=GROUP, status="active")
    } == {winner}
    after = store.metrics()
    assert after["definition_version_count"] == before["definition_version_count"] + 1


def test_conflicting_strength_is_preserved(tmp_path: Path) -> None:
    store = _merge_store(tmp_path)
    first = store.upsert_definition(build_definition(
        "Always use rtk for shell commands", kind="fact", rule_strength="must",
    ))
    second = store.upsert_definition(build_definition(
        "Always use rtk for shell commands", kind="procedure", rule_strength="should",
    ))
    result = reconcile_historical_duplicates(tmp_path, GROUP, store=store)
    assert result["merged_count"] == 0
    assert {item.status for item in store.list_definitions()} == {"active"}


def _seed_v2_duplicate(
    store: RuleV2Store,
    text: str,
    *,
    kind: str,
    definition_id: str,
    source_id: str,
    strength: str = "must",
    group: str = GROUP,
):
    definition = build_definition(text, kind=kind, rule_strength=strength)
    definition = definition.__class__(
        **{**definition.to_dict(), "definition_id": definition_id}
    )
    store.upsert_definition(definition)
    store.upsert_binding(build_binding(
        definition.definition_id,
        share_group_id=group,
        target_type="group",
        target_id=GROUP,
        binding_id=f"binding-{definition_id}",
    ))
    store.upsert_source_link(
        source_kind="native",
        share_group_id=group,
        memory_id=source_id,
        source_ref=source_id,
        source_revision="1",
        original_definition_id=definition.definition_id,
        canonical_definition_id=definition.definition_id,
        status="active",
    )
    store.record_evidence_ref({
        "evidence_id": f"evidence-{source_id}",
        "definition_id": definition.definition_id,
        "source_rule_id": source_id,
        "share_group_id": group,
        "evidence_ref": source_id,
    })
    return definition


def test_v2_reconciliation_uses_production_store_and_is_idempotent(tmp_path: Path) -> None:
    store = RuleV2Store(tmp_path)
    first = _seed_v2_duplicate(
        store, "Always use rtk for shell commands", kind="fact",
        definition_id="v2-fact", source_id="source-v2-fact",
    )
    second = _seed_v2_duplicate(
        store, "Always use rtk for shell commands", kind="procedure",
        definition_id="v2-procedure", source_id="source-v2-procedure",
    )
    before = store._read(lambda conn: {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "rule_definitions", "rule_definition_versions", "rule_definition_aliases",
            "rule_source_links", "rule_evidence_refs", "rule_receipt_refs",
        )
    })

    first_run = reconcile_historical_duplicates(tmp_path, GROUP, store=store)
    assert first_run["merged_count"] == 1
    assert store.get_definition(first.definition_id).status == "alias"
    assert store.get_definition(first.definition_id).superseded_by == second.definition_id
    assert store.resolve_canonical(first.definition_id) == second.definition_id
    assert {
        link["canonical_definition_id"]
        for link in store.list_source_links(share_group_id=GROUP, status="active")
    } == {second.definition_id}
    assert store._read(lambda conn: conn.execute(
        "SELECT COUNT(*) FROM rule_evidence_refs WHERE definition_id=?",
        (second.definition_id,),
    ).fetchone()[0]) == 2

    after_first = store._read(lambda conn: {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    })
    # The reversible fold records both winner and loser pre-state versions.
    assert after_first["rule_definition_versions"] == before["rule_definition_versions"] + 2
    assert after_first["rule_definition_aliases"] == before["rule_definition_aliases"] + 1
    assert after_first["rule_receipt_refs"] == before["rule_receipt_refs"] + 1
    assert after_first["rule_source_links"] == before["rule_source_links"]
    assert after_first["rule_evidence_refs"] == before["rule_evidence_refs"]
    second_run = reconcile_historical_duplicates(tmp_path, GROUP, store=store)
    after_second = store._read(lambda conn: {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    })
    assert second_run["merged_count"] == 0
    assert after_second == after_first


def test_v2_same_group_cross_audience_exact_is_folded_with_provenance(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    first = _seed_v2_duplicate(
        store, "Always use rtk for shell commands", kind="fact",
        definition_id="v2-audience-a", source_id="source-audience-a",
    )
    second = _seed_v2_duplicate(
        store, "Always use rtk for shell commands", kind="procedure",
        definition_id="v2-audience-b", source_id="source-audience-b",
    )
    store.upsert_binding(build_binding(
        second.definition_id,
        share_group_id=GROUP,
        target_type="group",
        target_id="different-group",
        binding_id="binding-v2-audience-b-extra",
    ))
    result = reconcile_historical_duplicates(tmp_path, GROUP, store=store)
    assert result["merged_count"] == 1
    assert result["conflict_count"] == 0
    assert store.get_definition(first.definition_id).status == "alias"
    assert store.get_definition(first.definition_id).superseded_by == second.definition_id
    assert store.get_definition(second.definition_id).status == "active"
    canonical = store.get_definition(second.definition_id)
    assert canonical is not None
    assert canonical.canonical_text.count("alwaysusertkforshellcommands") == 1

    active_bindings = store.list_bindings(share_group_id=GROUP, status="active")
    assert {binding.definition_id for binding in active_bindings} == {second.definition_id}
    assert {
        (binding.target_type, binding.target_id)
        for binding in active_bindings
    } == {("group", GROUP), ("group", "different-group")}
    assert {binding.binding_id for binding in active_bindings} >= {
        "binding-v2-audience-b",
        "binding-v2-audience-b-extra",
    }

    source_links = store.list_source_links(share_group_id=GROUP, status="active")
    assert {link["memory_id"] for link in source_links} >= {
        "source-audience-a",
        "source-audience-b",
    }
    assert {
        link["canonical_definition_id"] for link in source_links
    } == {second.definition_id}


def test_v2_reconciliation_ignores_definitions_unreachable_from_group(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    first = _seed_v2_duplicate(
        store, "Always use rtk for shell commands", kind="fact",
        definition_id="v2-outside-a", source_id="source-outside-a",
        group="other-group",
    )
    second = _seed_v2_duplicate(
        store, "Always use rtk for shell commands", kind="procedure",
        definition_id="v2-outside-b", source_id="source-outside-b",
        group="other-group",
    )
    result = reconcile_historical_duplicates(tmp_path, GROUP, store=store)
    assert result["merged_count"] == 0
    assert store.get_definition(first.definition_id).status == "active"
    assert store.get_definition(second.definition_id).status == "active"


def test_v2_reconciliation_preserves_cross_group_scope_difference(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    first = _seed_v2_duplicate(
        store, "Always use rtk for shell commands", kind="fact",
        definition_id="v2-cross-a", source_id="source-cross-a",
    )
    second = _seed_v2_duplicate(
        store, "Always use rtk for shell commands", kind="procedure",
        definition_id="v2-cross-b", source_id="source-cross-b",
    )
    store.upsert_binding(build_binding(
        first.definition_id,
        share_group_id="other-group",
        target_type="group",
        target_id="other-group",
        binding_id="binding-v2-cross-a-other",
    ))
    result = reconcile_historical_duplicates(tmp_path, GROUP, store=store)
    assert result["merged_count"] == 0
    assert result["conflict_count"] == 1
    assert store.get_definition(first.definition_id).status == "active"
    assert store.get_definition(second.definition_id).status == "active"


def test_v2_reconciliation_merges_exactly_equivalent_cross_group_scope(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    first = _seed_v2_duplicate(
        store, "Always use rtk for shell commands", kind="fact",
        definition_id="v2-equal-a", source_id="source-equal-a",
    )
    second = _seed_v2_duplicate(
        store, "Always use rtk for shell commands", kind="procedure",
        definition_id="v2-equal-b", source_id="source-equal-b",
    )
    for definition, suffix in ((first, "a"), (second, "b")):
        store.upsert_binding(build_binding(
            definition.definition_id,
            share_group_id="other-group",
            target_type="group",
            target_id="other-group",
            binding_id=f"binding-v2-equal-other-{suffix}",
        ))
        store.upsert_source_link(
            source_kind="native",
            share_group_id="other-group",
            memory_id=f"source-equal-other-{suffix}",
            source_ref=f"source-equal-other-{suffix}",
            original_definition_id=definition.definition_id,
            canonical_definition_id=definition.definition_id,
            status="active",
        )
    result = reconcile_historical_duplicates(tmp_path, GROUP, store=store)
    assert result["merged_count"] == 1
    assert store.get_definition(first.definition_id).status == "alias"


def test_v2_historical_duplicate_undo_restores_production_relations(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    first = _seed_v2_duplicate(
        store, "Always use rtk for shell commands", kind="fact",
        definition_id="v2-undo-a", source_id="source-undo-a",
    )
    second = _seed_v2_duplicate(
        store, "Always use rtk for shell commands", kind="procedure",
        definition_id="v2-undo-b", source_id="source-undo-b",
    )
    merged = reconcile_historical_duplicates(tmp_path, GROUP, store=store)
    decision_id = merged["details"][0]["decision_id"]
    undone = store.undo_historical_duplicate(decision_id, share_group_id=GROUP)
    assert undone["status"] == "undone"
    assert store.get_definition(first.definition_id).status == "active"
    assert store.get_definition(second.definition_id).status == "active"
    assert store.get_definition(first.definition_id).superseded_by == ""
    assert store.resolve_canonical(first.definition_id) == first.definition_id
    assert {
        link["canonical_definition_id"]
        for link in store.list_source_links(share_group_id=GROUP, status="active")
    } == {first.definition_id, second.definition_id}
    again = store.undo_historical_duplicate(decision_id, share_group_id=GROUP)
    assert again["status"] == "undone"
    assert again.get("already_undone") is True
    rerun = reconcile_historical_duplicates(tmp_path, GROUP, store=store)
    assert rerun["merged_count"] == 1


def test_rule_creation_undo_dispatches_historical_fold(tmp_path: Path) -> None:
    store = RuleV2Store(tmp_path)
    first = _seed_v2_duplicate(
        store, "Always use rtk for shell commands", kind="fact",
        definition_id="v2-dispatch-a", source_id="source-dispatch-a",
    )
    second = _seed_v2_duplicate(
        store, "Always use rtk for shell commands", kind="procedure",
        definition_id="v2-dispatch-b", source_id="source-dispatch-b",
    )
    merged = reconcile_historical_duplicates(tmp_path, GROUP, store=store)
    decision_id = merged["details"][0]["decision_id"]
    service = RuleCreationService(tmp_path, GROUP, is_admin=True)
    undone = service.undo_rule_decision(decision_id, is_admin=True)
    assert undone.status == "undone"
    assert store.get_definition(first.definition_id).status == "active"
    assert store.get_definition(second.definition_id).status == "active"


def test_v2_historical_undo_failure_rolls_back_all_inverse_updates(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    first = _seed_v2_duplicate(
        store, "Always use rtk for shell commands", kind="fact",
        definition_id="v2-undo-fault-a", source_id="source-undo-fault-a",
    )
    second = _seed_v2_duplicate(
        store, "Always use rtk for shell commands", kind="procedure",
        definition_id="v2-undo-fault-b", source_id="source-undo-fault-b",
    )
    merged = reconcile_historical_duplicates(tmp_path, GROUP, store=store)
    decision_id = merged["details"][0]["decision_id"]
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "CREATE TRIGGER injected_historical_undo_failure "
            "AFTER UPDATE OF status ON rule_bindings "
            "WHEN NEW.status='active' BEGIN SELECT RAISE(ABORT, 'injected_undo_failure'); END"
        )
    try:
        try:
            store.undo_historical_duplicate(decision_id, share_group_id=GROUP)
        except sqlite3.Error as exc:
            assert "injected_undo_failure" in str(exc)
        else:
            raise AssertionError("undo should fail closed")
    finally:
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("DROP TRIGGER injected_historical_undo_failure")
    assert store.get_definition(first.definition_id).status == "alias"
    assert store.get_definition(second.definition_id).status == "active"
    assert store.resolve_canonical(first.definition_id) == second.definition_id


def test_v2_reconciliation_rolls_back_on_receipt_failure(tmp_path: Path, monkeypatch) -> None:
    store = RuleV2Store(tmp_path)
    first = _seed_v2_duplicate(
        store, "Always use rtk for shell commands", kind="fact",
        definition_id="v2-atomic-a", source_id="source-atomic-a",
    )
    second = _seed_v2_duplicate(
        store, "Always use rtk for shell commands", kind="procedure",
        definition_id="v2-atomic-b", source_id="source-atomic-b",
    )
    before = store._read(lambda conn: {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "rule_definitions", "rule_definition_versions", "rule_definition_aliases",
            "rule_source_links", "rule_evidence_refs", "rule_receipt_refs",
        )
    })
    original = store.record_receipt

    def fail_once(value):
        raise RuntimeError("receipt_sink_failed")

    monkeypatch.setattr(store, "record_receipt", fail_once)
    try:
        reconcile_historical_duplicates(tmp_path, GROUP, store=store)
    except RuntimeError as exc:
        assert str(exc) == "receipt_sink_failed"
    else:
        raise AssertionError("reconciliation should fail closed")
    monkeypatch.setattr(store, "record_receipt", original)
    assert store.get_definition(first.definition_id).status == "active"
    assert store.get_definition(second.definition_id).status == "active"
    after = store._read(lambda conn: {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    })
    assert after == before


def test_settle_native_snapshot_is_formal_reconciliation_entry(
    tmp_path: Path, monkeypatch,
) -> None:
    from memoryguard import rule_reconciliation as reconciliation

    store = RuleV2Store(tmp_path)
    calls = []
    original = reconciliation.reconcile_historical_duplicates

    def tracked(*args, **kwargs):
        calls.append(kwargs.get("store"))
        return original(*args, **kwargs)

    monkeypatch.setattr(reconciliation, "reconcile_historical_duplicates", tracked)
    try:
        reconciliation.settle_native_canonical_snapshot(tmp_path, GROUP, store=store)
    except RuntimeError as exc:
        assert str(exc).startswith("canonical_snapshot_not_settleable:")
    assert calls and calls[0] is store


def test_imported_prepare_job_does_not_block_native_snapshot_settlement(
    tmp_path: Path,
) -> None:
    """A migrated prepare row is provenance, not a live V2 coordinator lock."""

    store = RuleV2Store(tmp_path)
    definition = _seed_v2_duplicate(
        store,
        "Always use rtk for shell commands",
        kind="procedure",
        definition_id="v2-prepare-recovery",
        source_id="source-prepare-recovery",
    )
    store.record_reconciliation_job({
        "job_id": "prepare-stuck",
        "share_group_id": GROUP,
        "migration_id": "prepare-stuck",
        "phase": "write_canonical",
        "status": "applying",
        "source_digest": "old-source",
        "last_error": "process interrupted",
    })

    before = canonical_reconciliation_status(tmp_path, GROUP, store=store)
    assert before["canonical_ready"] is False
    assert "reconciliation_in_flight" not in before["failures"]
    assert before["checks"]["historical_reconciliation_jobs"] == 1

    settled = settle_native_canonical_snapshot(tmp_path, GROUP, store=store)
    assert settled["canonical_ready"] is True
    assert settled["checks"]["historical_reconciliation_jobs"] == 1
    assert store.get_definition(definition.definition_id).status == "active"


def test_unscoped_native_reconciliation_job_still_blocks_snapshot(
    tmp_path: Path,
) -> None:
    store = RuleV2Store(tmp_path)
    _seed_v2_duplicate(
        store,
        "Always use rtk for shell commands",
        kind="procedure",
        definition_id="v2-live-job",
        source_id="source-live-job",
    )
    store.record_reconciliation_job({
        "job_id": "native-live-job",
        "share_group_id": GROUP,
        "phase": "write_canonical",
        "status": "applying",
    })

    result = canonical_reconciliation_status(tmp_path, GROUP, store=store)
    assert result["checks"]["reconciliation_in_flight"] == 1
    assert "reconciliation_in_flight" in result["failures"]
    try:
        settle_native_canonical_snapshot(tmp_path, GROUP, store=store)
    except RuntimeError as exc:
        assert "canonical_snapshot_not_settleable" in str(exc)
    else:
        raise AssertionError("a live native reconciliation job must block settlement")
