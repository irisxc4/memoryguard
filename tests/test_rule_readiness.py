"""Pure readiness and trusted-maturity policy tests."""
from __future__ import annotations

from memoryguard.rule_merge_policy import (
    READINESS_COMPONENTS,
    build_maturity_snapshot,
    build_readiness_snapshot,
)


def _trusted_events(count: int, sessions: int, agents: int, projects: int):
    return {
        "events": [
            {
                "session_trusted": True,
                "session_id": f"s{i % sessions}",
                "agent_instance_id": f"a{i % agents}",
                "project_ref": f"p{i % projects}",
                "outcome": "followed",
            }
            for i in range(count)
        ]
    }


def test_maturity_uses_trusted_total_and_all_distinct_dimensions():
    validated = build_maturity_snapshot(
        runtime=_trusted_events(10, sessions=2, agents=2, projects=2),
    )
    assert validated["state"] == "validated"
    assert validated["trusted_total"] == 10

    trusted = build_maturity_snapshot(
        runtime=_trusted_events(20, sessions=5, agents=3, projects=3),
    )
    assert trusted["state"] == "trusted"
    assert build_maturity_snapshot(runtime={
        "trusted_total": 20,
        "trusted_sessions": 5,
        "trusted_agents": 3,
        "trusted_projects": 3,
    })["state"] == "trusted"


def test_empty_or_untrusted_observations_do_not_establish_maturity():
    empty = build_maturity_snapshot(runtime=[])
    assert empty["state"] == "observing"
    untrusted = build_maturity_snapshot(runtime={
        "events": [{
            "session_trusted": False,
            "session_id": "s1",
            "agent_instance_id": "a1",
            "project_ref": "p1",
        }] * 20,
    })
    assert untrusted["trusted_total"] == 0
    assert untrusted["state"] == "observing"


def test_readiness_snapshot_is_fixed_deterministic_and_preserves_zero():
    kwargs = {
        "definition": {
            "maturity_state": "validated",
            "created_at": "2020-01-01T00:00:00+00:00",
        },
        "evidence": [{"confidence": 0.0, "agent_instance_id": "a1",
                      "project_ref": "p1"}],
        "runtime": {"trusted_followed": 0, "trusted_total": 0},
        "reputation": {},
        "project": {},
        "similarity": {"duplicate_score": 0.9},
    }
    first = build_readiness_snapshot(**kwargs)
    second = build_readiness_snapshot(**kwargs)
    assert tuple(first["components"]) == READINESS_COMPONENTS
    assert first["components"]["evidence"] == 0.0
    assert "runtime.success" in first["unknown"]
    assert first["digest"] == second["digest"]
