from __future__ import annotations

import json

import pytest

from memoryguard.retrieval_v2 import (
    RecallPlanner,
    RecallRequest,
    RecallScope,
    StaticLayerPort,
)


def scope(**overrides):
    value = {
        "workspace_id": "ws-1",
        "share_group_id": "group-1",
        "agent_instance_id": "agent-1",
        "project_ref": "project-1",
        "provider": "codex",
        "runtime_role": "worker",
    }
    value.update(overrides)
    return RecallScope(**value)


def item(item_id: str, **overrides):
    value = {
        "item_id": item_id,
        "workspace_id": "ws-1",
        "share_group_id": "group-1",
        "agent_instance_id": "agent-1",
        "project_ref": "project-1",
        "provider": "codex",
        "runtime_role": "worker",
        "summary": item_id,
        "confidence": 0.5,
        "relevance": 0.5,
    }
    value.update(overrides)
    return value


def request(*, layers=None, **overrides):
    return RecallRequest(
        query="alpha",
        scope=scope(),
        layers=tuple(layers or ("working", "rules", "content_reference", "knowledge", "codegraph", "skill")),
        **overrides,
    )


def test_scope_acl_filters_every_dimension_and_does_not_leak_denied_ids():
    rows = [
        item("ok"),
        item("other-agent", agent_instance_id="agent-2"),
        item("other-project", project_ref="project-2"),
        item("other-provider", provider="other"),
        item("other-runtime", runtime_role="server"),
        item("other-group", share_group_id="group-2"),
        item("other-workspace", workspace_id="ws-2"),
        {"item_id": "missing-scope", "summary": "secret"},
    ]
    plan = RecallPlanner([StaticLayerPort("working", rows)]).plan(request(layers=("working",)))
    assert [decision.item_id for decision in plan.selected] == ["ok"]
    assert all(decision.reason == "scope_denied" for decision in plan.excluded)
    assert all("other-" not in decision.item_id and "missing" not in decision.item_id for decision in plan.excluded)


def test_only_trusted_rules_can_produce_mandatory_or_enforceable():
    rows = [item("malicious", mandatory=True), item("normal", confidence=0.9)]
    plan = RecallPlanner([StaticLayerPort("working", rows)]).plan(request(layers=("working",)))
    assert all(decision.trust == "relevant" for decision in plan.selected)
    assert any("untrusted_mandatory" in decision.reason for decision in plan.selected)

    trusted = RecallPlanner([StaticLayerPort("rules", [item("must", mandatory=True)])]).plan(request(layers=("rules",)))
    assert trusted.selected[0].trust == "mandatory"

    malicious_rule = RecallPlanner([StaticLayerPort("rules", [item("must", mandatory=True)], trusted=False)]).plan(
        request(layers=("rules",))
    )
    assert malicious_rule.selected[0].trust == "relevant"


def test_history_and_content_never_return_body_or_transcript():
    rows = [
        item(
            "history-1",
            summary="safe summary",
            body="private transcript body",
            text="private text",
            transcript="private transcript",
            canonical_hash="hash-1",
        )
    ]
    plan = RecallPlanner([StaticLayerPort("history", rows)]).plan(request(layers=("history",)))
    data = plan.to_dict()
    encoded = json.dumps(data, ensure_ascii=False).lower()
    assert "private transcript body" not in encoded
    assert "private text" not in encoded
    assert "private transcript" not in encoded
    assert data["selected"][0]["summary"] == "safe summary"
    assert data["selected"][0]["source_digest"] == "hash-1"
    assert not any(key in data["selected"][0] for key in ("body", "text", "raw", "transcript"))


def test_missing_future_ports_are_explicit_not_configured():
    plan = RecallPlanner([StaticLayerPort("working", [item("one")])]).plan(request())
    for layer in ("knowledge", "codegraph", "skill"):
        assert plan.layer_status[layer] == "NOT_CONFIGURED"
        assert any(decision.layer == layer and decision.reason == "NOT_CONFIGURED" for decision in plan.excluded)


def test_dedupe_filter_and_deterministic_sorting_are_idempotent():
    rows = [
        item("low", summary="alpha low", relevance=0.2, confidence=0.2, recency_score=0.1, canonical_hash="same"),
        item("winner", summary="alpha winner", relevance=0.9, confidence=0.9, recency_score=0.9, canonical_hash="same"),
        item("locked", locked=True),
        item("deleted", status="deleted"),
        item("conflict", conflict=True),
    ]
    planner = RecallPlanner([StaticLayerPort("atoms", rows)])
    first = planner.plan(request(layers=("atoms",)))
    second = planner.plan(request(layers=("atoms",)))
    assert first.to_dict() == second.to_dict()
    assert [decision.item_id for decision in first.selected] == ["winner"]
    assert any(decision.reason.startswith("duplicate_of:winner") for decision in first.excluded)
    assert {decision.reason for decision in first.excluded} >= {"locked", "deleted", "conflict"}


def test_mandatory_budget_overflow_fails_closed_without_expansion():
    rows = [item("must-1", mandatory=True, summary="12345"), item("must-2", mandatory=True, summary="67890")]
    plan = RecallPlanner([StaticLayerPort("rules", rows)]).plan(
        request(layers=("rules",), budget_items=1, budget_chars=5)
    )
    assert plan.status == "blocked"
    assert plan.mandatory_overflow is True
    assert plan.selected == ()
    assert all(decision.action == "exclude" for decision in plan.decisions)


def test_budget_is_hard_and_plan_digest_stable():
    rows = [item(str(index), summary="alpha", relevance=1.0) for index in range(5)]
    planner = RecallPlanner([StaticLayerPort("working", rows)])
    plan = planner.plan(request(layers=("working",), budget_items=2, budget_chars=100))
    assert len(plan.selected) == 2
    assert plan.counts["selected"] == 2
    assert plan.digest == planner.plan(request(layers=("working",), budget_items=2, budget_chars=100)).digest


def test_every_decision_has_reason_and_evidence_refs_are_preserved():
    plan = RecallPlanner(
        [
            StaticLayerPort(
                "working",
                [item("evidence", evidence_refs=["e-2", "e-1"], summary="alpha")],
            )
        ]
    ).plan(request(layers=("working",)))
    decision = plan.selected[0]
    assert decision.reason
    assert decision.evidence_refs == ("e-1", "e-2")


def test_scope_mapping_alias_conflicts_fail_closed():
    with pytest.raises(ValueError, match="conflicting scope alias"):
        RecallRequest.from_value(
            {
                "query": "alpha",
                "workspace_id": "ws-1",
                "share_group_id": "group-1",
                "group_id": "group-2",
            }
        )


@pytest.mark.parametrize(
    ("top_alias", "top_value", "nested_alias", "nested_value"),
    [
        ("workspace", "ws-top", "workspace_id", "ws-nested"),
        ("agent", "agent-top", "agent_instance_id", "agent-nested"),
        ("project", "project-top", "project_ref", "project-nested"),
        ("group", "group-top", "share_group_id", "group-nested"),
        ("provider", "provider-top", "provider", "provider-nested"),
        ("runtime", "runtime-top", "runtime_role", "runtime-nested"),
    ],
)
def test_nested_scope_and_top_level_alias_conflicts_fail_closed(top_alias, top_value, nested_alias, nested_value):
    with pytest.raises(ValueError, match="conflicting scope alias"):
        RecallRequest.from_value(
            {
                "query": "alpha",
                top_alias: top_value,
                "scope": {
                    "workspace_id": "ws-1",
                    "share_group_id": "group-1",
                    nested_alias: nested_value,
                },
            }
        )


def test_nested_scope_and_matching_top_level_aliases_merge_without_widening():
    request_value = RecallRequest.from_value(
        {
            "query": "alpha",
            "workspace": "ws-1",
            "agent": "agent-1",
            "project": "project-1",
            "group": "group-1",
            "provider": "codex",
            "runtime": "worker",
            "scope": {
                "workspace_id": "ws-1",
                "share_group_id": "group-1",
                "agent_instance_id": "agent-1",
                "project_ref": "project-1",
                "provider": "codex",
                "runtime_role": "worker",
            },
        }
    )
    assert request_value.scope == scope()


def test_non_mapping_nested_scope_is_rejected():
    with pytest.raises(TypeError, match="scope must be RecallScope or mapping"):
        RecallRequest.from_value({"query": "alpha", "scope": ["ws-1", "group-1"]})


def test_candidate_target_scope_conflict_is_opaque_and_fail_closed():
    conflicting = item(
        "target-secret",
        scope={"target_type": "agent", "target_id": "agent-2", "agent_instance_id": "agent-1"},
    )
    planner = RecallPlanner([StaticLayerPort("working", [conflicting])])
    plan = planner.plan(request(layers=("working",)))
    assert plan.selected == ()
    assert len(plan.excluded) == 1
    denied = plan.excluded[0]
    assert denied.reason == "scope_denied"
    assert "target-secret" not in denied.item_id
    assert "agent-2" not in denied.item_id


@pytest.mark.parametrize(
    "scope_value",
    [
        "agent:agent-2",
        {"scope_type": "agent", "scope_id": "agent-2"},
        {"scope_type": "agent", "scope_id": "agent-2", "type": "agent", "id": "agent-1"},
        {"workspace_id": "ws-1", "scope": {"scope_type": "agent", "scope_id": "agent-2"}},
        ["agent", "agent-2"],
    ],
)
def test_scope_aliases_and_non_mapping_scope_never_widen_access(scope_value):
    candidate = item("scope-secret", scope=scope_value)
    plan = RecallPlanner([StaticLayerPort("working", [candidate])]).plan(request(layers=("working",)))
    assert plan.selected == ()
    assert plan.excluded[0].reason == "scope_denied"
    assert "scope-secret" not in plan.excluded[0].item_id


@pytest.mark.parametrize(
    "status_key,status_value,expected",
    [
        ("status", "conflicted", "conflicted"),
        ("state", "locked", "locked"),
        ("lifecycle", "quarantine", "quarantine"),
        ("lifecycle_status", "deleted", "deleted"),
        ("status", "active", "locked"),
    ],
)
def test_status_or_boolean_lifecycle_markers_are_denied(status_key, status_value, expected):
    values = {status_key: status_value}
    if status_key == "status" and status_value == "active":
        values["locked"] = True
    plan = RecallPlanner([StaticLayerPort("atoms", [item("secret", **values)])]).plan(request(layers=("atoms",)))
    assert plan.selected == ()
    assert plan.excluded[0].reason == expected


def test_unknown_layers_and_ports_fail_closed_with_diagnostics():
    unknown = StaticLayerPort("evil", [item("must", mandatory=True)])
    planner = RecallPlanner([unknown])
    assert planner.diagnostics == {"evil": "UNKNOWN_LAYER"}
    plan = planner.plan(request(layers=("evil",)))
    assert plan.layer_status["evil"] == "UNKNOWN_LAYER"
    assert plan.selected == ()
    assert plan.excluded[0].reason == "UNKNOWN_LAYER"
