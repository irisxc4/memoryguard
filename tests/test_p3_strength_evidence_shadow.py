from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rule_evidence import build_evidence, build_negative_evidence
from memoryguard.rule_evidence_ledger import list_contributions, list_effective
from memoryguard.rule_merge_store import RuleMergeStore
from memoryguard.schema_v3 import EffectiveAgentContext


def _definition(name: str, definition_id: str):
    return build_definition(name, definition_id=definition_id)


def _seed_binding(store: RuleMergeStore, definition_id: str, source: str):
    binding = build_binding(
        definition_id,
        share_group_id="group-1",
        target_type="agent",
        target_id=source,
        project_ref="project-1",
        provider="codex",
        runtime_role="developer",
        effect="include",
        priority=10 if source == "source-a" else 20,
        owner_agent_id=source,
    )
    store.replace_source_contributions("group-1", source, [binding])
    store.upsert_source_link(
        share_group_id="group-1",
        memory_id=source,
        original_definition_id=definition_id,
        canonical_definition_id=definition_id,
    )
    return binding


def _ledger_rows(store: RuleMergeStore):
    conn = store._db()
    try:
        return list_contributions(conn), list_effective(conn)
    finally:
        conn.close()


def test_evolve_rehomes_bindings_and_contributions_atomically(tmp_path):
    store = RuleMergeStore(tmp_path)
    old = _definition("old rule", "definition-old")
    new = _definition("new rule", "definition-new")
    store.upsert_definition(old)
    first = _seed_binding(store, old.definition_id, "source-a")
    second = _seed_binding(store, old.definition_id, "source-b")

    before_audiences = sorted(
        binding.audience_identity()
        for binding in store.list_bindings(
            definition_id=old.definition_id, status="active",
        )
    )
    before_contributions = sorted(
        (
            row["source_memory_id"], row["legacy_assignment_hash"],
            row["audience"], row["active"], row["status"],
        )
        for row in store.list_binding_contributions(active=True)
    )

    result = store.evolve_definition_atomic(
        old_definition_id=old.definition_id,
        new_definition=new,
        old_strength="observation",
        new_strength="must",
        change_reason="accepted evidence",
    )

    assert result["new_definition_id"] == new.definition_id
    assert store.get_definition(old.definition_id).status == "superseded"
    assert store.get_definition(new.definition_id).status == "active"
    assert {
        binding.binding_id
        for binding in store.list_bindings(
            definition_id=old.definition_id, status=None,
        )
    } == {first.binding_id, second.binding_id}
    assert all(
        binding.status == "revoked"
        for binding in store.list_bindings(
            definition_id=old.definition_id, status=None,
        )
    )

    new_bindings = store.list_bindings(
        definition_id=new.definition_id, status="active",
    )
    assert {binding.binding_id for binding in new_bindings}.isdisjoint(
        {first.binding_id, second.binding_id}
    )
    assert sorted(binding.audience_identity() for binding in new_bindings) == before_audiences

    active_contributions = store.list_binding_contributions(active=True)
    assert all(
        row["definition_id"] == new.definition_id
        and row["binding_id"] in {binding.binding_id for binding in new_bindings}
        for row in active_contributions
    )
    assert sorted(
        (
            row["source_memory_id"], row["legacy_assignment_hash"],
            row["audience"], row["active"], row["status"],
        )
        for row in active_contributions
    ) == before_contributions
    assert store.get_source_link("group-1", "source-a")[
        "canonical_definition_id"
    ] == new.definition_id
    assert store.get_source_link("group-1", "source-b")[
        "canonical_definition_id"
    ] == new.definition_id
    assert store.metrics()["binding_contribution_diff"] == 0


def test_evolve_rolls_back_definition_rehome_failure(tmp_path, monkeypatch):
    store = RuleMergeStore(tmp_path)
    old = _definition("old rule", "definition-old")
    new = _definition("new rule", "definition-new")
    store.upsert_definition(old)
    old_binding = _seed_binding(store, old.definition_id, "source-a")
    before = store.list_binding_contributions(active=True)

    def fail(cls, conn, binding_ids):
        raise RuntimeError("injected rehome failure")

    monkeypatch.setattr(
        RuleMergeStore,
        "_materialize_affected_bindings_conn",
        classmethod(fail),
    )
    with pytest.raises(RuntimeError, match="injected rehome failure"):
        store.evolve_definition_atomic(
            old_definition_id=old.definition_id,
            new_definition=new,
            old_strength="observation",
            new_strength="must",
        )

    assert store.get_definition(new.definition_id) is None
    assert store.get_definition(old.definition_id).status == "active"
    assert store.list_bindings(
        definition_id=old.definition_id, status="active",
    )[0].binding_id == old_binding.binding_id
    assert store.list_binding_contributions(active=True) == before
    assert store.get_source_link("group-1", "source-a")[
        "canonical_definition_id"
    ] == old.definition_id


def test_public_positive_evidence_keeps_runner_up_and_restores_it(tmp_path):
    store = RuleMergeStore(tmp_path)
    definition = _definition("positive evidence", "definition-1")
    store.upsert_definition(definition)
    low = replace(
        build_evidence(
            definition_id=definition.definition_id,
            source_rule_id="low-source",
            agent_instance_id="agent-low",
            project_ref="project-1",
            session_id="session-low",
            content="same observation",
        ),
        independence_key="same-observation",
        feedback_authority=10,
    )
    high = replace(
        build_evidence(
            definition_id=definition.definition_id,
            source_rule_id="high-source",
            agent_instance_id="agent-high",
            project_ref="project-1",
            session_id="session-high",
            content="same observation",
        ),
        independence_key="same-observation",
        feedback_authority=20,
    )

    store.upsert_evidence(low)
    store.upsert_evidence(high)
    contributions, effective = _ledger_rows(store)
    assert {item.source_evidence_id for item in contributions} == {
        low.evidence_id, high.evidence_id,
    }
    by_contribution_id = {
        item.contribution_id: item.source_evidence_id for item in contributions
    }
    assert by_contribution_id[effective[0].winner_contribution_id] == high.evidence_id
    assert [item.evidence_id for item in store.list_evidence()] == [high.evidence_id]

    store.deactivate_source_evidence("high-source", "agent-high")
    contributions, effective = _ledger_rows(store)
    by_contribution_id = {
        item.contribution_id: item.source_evidence_id for item in contributions
    }
    assert by_contribution_id[effective[0].winner_contribution_id] == low.evidence_id
    assert [item.evidence_id for item in store.list_evidence()] == [low.evidence_id]


def test_public_negative_evidence_uses_same_fallback_projection(tmp_path):
    store = RuleMergeStore(tmp_path)
    definition = _definition("negative evidence", "definition-1")
    store.upsert_definition(definition)
    low = replace(
        build_negative_evidence(
            definition_id=definition.definition_id,
            source_rule_id="negative-low",
            agent_instance_id="agent-low",
            project_ref="project-1",
            session_id="session-low",
            content="counter example",
        ),
        independence_key="same-negative-observation",
        feedback_authority=10,
    )
    high = replace(
        build_negative_evidence(
            definition_id=definition.definition_id,
            source_rule_id="negative-high",
            agent_instance_id="agent-high",
            project_ref="project-1",
            session_id="session-high",
            content="counter example",
        ),
        independence_key="same-negative-observation",
        feedback_authority=20,
    )

    store.upsert_negative_evidence(low)
    store.upsert_negative_evidence(high)
    assert [item.evidence_id for item in store.list_negative_evidence()] == [
        high.evidence_id
    ]
    store.deactivate_source_evidence("negative-high", "agent-high")
    assert [item.evidence_id for item in store.list_negative_evidence()] == [
        low.evidence_id
    ]


def test_store_upgrade_imports_active_and_inactive_legacy_evidence(tmp_path):
    store = RuleMergeStore(tmp_path)
    definition = _definition("legacy evidence", "definition-legacy")
    store.upsert_definition(definition)
    conn = store._db()
    try:
        conn.execute(
            """
            INSERT INTO rule_evidence (
                evidence_id, definition_id, source_rule_id, agent_instance_id,
                project_ref, confidence, observed_at, independence_key, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("legacy-low", definition.definition_id, "legacy-low-source", "a1",
             "p1", 1.0, "2026-08-03T00:00:00Z", "legacy-fact", 0),
        )
        conn.execute(
            """
            INSERT INTO rule_evidence (
                evidence_id, definition_id, source_rule_id, agent_instance_id,
                project_ref, confidence, observed_at, independence_key, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("legacy-high", definition.definition_id, "legacy-high-source", "a2",
             "p1", 1.0, "2026-08-03T01:00:00Z", "legacy-fact", 1),
        )
        conn.commit()
    finally:
        conn.close()

    reopened = RuleMergeStore(tmp_path)
    contributions, effective = _ledger_rows(reopened)
    imported = {
        item.source_evidence_id: item.active
        for item in contributions
        if item.source_evidence_id in {"legacy-low", "legacy-high"}
    }
    assert imported == {"legacy-low": False, "legacy-high": True}
    by_contribution_id = {
        item.contribution_id: item.source_evidence_id for item in contributions
    }
    assert by_contribution_id[effective[0].winner_contribution_id] == "legacy-high"
    assert [item.evidence_id for item in reopened.list_evidence()] == [
        "legacy-high"
    ]


def test_standalone_migration_imports_legacy_evidence_history(tmp_path):
    from memoryguard.migrations.rule_intelligence_v2 import migrate

    db_path = tmp_path / "legacy-v2.sqlite3"
    migrate(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            """
            INSERT INTO rule_definitions (
                definition_id, canonical_text, normalized_intent, rule_kind,
                polarity, semantic_hash, parameter_schema, status, confidence,
                revision, rule_strength, maturity_state, created_at, updated_at,
                superseded_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("definition-standalone", "legacy", "legacy", "workflow", "positive",
             "semantic", "{}", "active", 1.0, 1, "observation", "observing",
             "2026-08-03T00:00:00Z", "2026-08-03T00:00:00Z", ""),
        )
        for evidence_id, observed_at, active in (
            ("standalone-low", "2026-08-03T00:00:00Z", 0),
            ("standalone-high", "2026-08-03T01:00:00Z", 1),
        ):
            conn.execute(
                """
                INSERT INTO rule_evidence (
                    evidence_id, definition_id, source_rule_id, agent_instance_id,
                    project_ref, content_hash, confidence, observed_at,
                    independence_key, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (evidence_id, "definition-standalone", evidence_id, "agent",
                 "project", evidence_id + "-content", 1.0, observed_at,
                 "standalone-fact", active),
            )
        conn.commit()
    finally:
        conn.close()

    migrate(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT source_evidence_id, active "
            "FROM rule_evidence_contributions ORDER BY source_evidence_id"
        ).fetchall()
        assert [(row["source_evidence_id"], bool(row["active"])) for row in rows] == [
            ("standalone-high", True),
            ("standalone-low", False),
        ]
        effective = conn.execute(
            "SELECT winner_contribution_id FROM rule_evidence_effective"
        ).fetchall()
        assert len(effective) == 1
        assert effective[0]["winner_contribution_id"]
    finally:
        conn.close()


def test_deleting_public_winner_restores_ledger_runner_up(tmp_path):
    store = RuleMergeStore(tmp_path)
    definition = _definition("delete evidence", "definition-delete")
    store.upsert_definition(definition)
    low = replace(
        build_evidence(
            definition_id=definition.definition_id,
            source_rule_id="delete-low",
            agent_instance_id="agent-low",
            content="same delete observation",
        ),
        independence_key="delete-observation",
        feedback_authority=10,
    )
    high = replace(
        build_evidence(
            definition_id=definition.definition_id,
            source_rule_id="delete-high",
            agent_instance_id="agent-high",
            content="same delete observation",
        ),
        independence_key="delete-observation",
        feedback_authority=20,
    )
    store.upsert_evidence(low)
    store.upsert_evidence(high)
    store.delete_evidence(high.evidence_id)
    assert [item.evidence_id for item in store.list_evidence()] == [low.evidence_id]


def test_shadow_verify_ignores_nonmatching_audience_in_current_group(tmp_path):
    store = RuleMergeStore(tmp_path)
    definition = _definition("other agent", "definition-other-agent")
    store.upsert_definition(definition)
    store.upsert_binding(build_binding(
        definition.definition_id,
        share_group_id="group-1",
        target_type="agent",
        target_id="agent-other",
    ))
    store.upsert_evidence(build_evidence(
        definition_id=definition.definition_id,
        source_rule_id="other-memory",
        agent_instance_id="agent-other",
        content="other agent",
    ))
    result = store.shadow_verify(
        EffectiveAgentContext(agent_instance_id="agent-current", share_group_id="group-1"),
        [],
    )
    assert result == {"missing": [], "extra": [], "permission_diff": 0}


@pytest.mark.parametrize(
    ("target_type", "target_id", "context_provider"),
    [("system", "", ""), ("group", "group-1", ""), ("provider", "codex", "codex")],
)
def test_shadow_verify_matches_migrated_broad_audiences(
    tmp_path, target_type, target_id, context_provider,
):
    store = RuleMergeStore(tmp_path)
    definition = _definition("shadow rule", "definition-shadow")
    store.upsert_definition(definition)
    store.upsert_binding(build_binding(
        definition.definition_id,
        share_group_id="group-1",
        target_type=target_type,
        target_id=target_id,
        provider=context_provider,
        created_by="manual",
    ))
    store.upsert_evidence(build_evidence(
        definition_id=definition.definition_id,
        source_rule_id="memory-1",
        agent_instance_id="agent-1",
        project_ref="project-1",
        session_id="session-1",
        content="shadow rule",
    ))
    result = store.shadow_verify(
        EffectiveAgentContext(
            agent_instance_id="agent-1",
            share_group_id="group-1",
            provider=context_provider,
        ),
        [("memory-1", [{"target_type": target_type, "target_id": target_id}])],
    )
    assert result == {"missing": [], "extra": [], "permission_diff": 0}


def test_shadow_verify_uses_record_priority_for_exact_legacy_audience(tmp_path):
    store = RuleMergeStore(tmp_path)
    definition = _definition("shadow rule", "definition-shadow")
    store.upsert_definition(definition)
    store.upsert_binding(build_binding(
        definition.definition_id,
        share_group_id="group-1",
        target_type="agent",
        target_id="agent-1",
        priority=10,
    ))
    store.upsert_evidence(build_evidence(
        definition_id=definition.definition_id,
        source_rule_id="memory-1",
        agent_instance_id="agent-1",
        content="shadow rule",
    ))
    legacy = [("memory-1", [{
        "target_type": "agent",
        "target_id": "agent-1",
        "effect": "include",
    }])]
    context = EffectiveAgentContext(
        agent_instance_id="agent-1",
        share_group_id="group-1",
    )
    result = store.shadow_verify(
        context,
        legacy,
        legacy_priorities={"memory-1": 10},
    )
    assert result == {"missing": [], "extra": [], "permission_diff": 0}


def test_shadow_verify_ignores_other_groups_and_detects_audience_changes(tmp_path):
    store = RuleMergeStore(tmp_path)
    definition = _definition("shadow scope", "definition-shadow")
    other = _definition("other group", "definition-other")
    store.upsert_definition(definition)
    store.upsert_definition(other)
    store.upsert_binding(build_binding(
        definition.definition_id, share_group_id="group-1",
        target_type="agent", target_id="agent-1", priority=1,
    ))
    store.upsert_binding(build_binding(
        other.definition_id, share_group_id="group-2",
        target_type="system",
    ))
    store.upsert_evidence(build_evidence(
        definition_id=definition.definition_id, source_rule_id="memory-1",
        agent_instance_id="agent-1", content="shadow scope",
    ))
    legacy = [("memory-1", [{
        "target_type": "agent", "target_id": "agent-1",
        "effect": "include", "priority_override": 1,
    }])]
    context = EffectiveAgentContext(
        agent_instance_id="agent-1", share_group_id="group-1",
    )
    assert store.shadow_verify(context, legacy)["permission_diff"] == 0

    store.upsert_binding(build_binding(
        definition.definition_id, share_group_id="group-1",
        target_type="agent", target_id="agent-1", effect="exclude", priority=2,
    ))
    assert store.shadow_verify(context, legacy)["permission_diff"] > 0
