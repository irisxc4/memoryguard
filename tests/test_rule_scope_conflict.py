"""Regression coverage for the conflict scope-composition witness."""

from __future__ import annotations

import json
from pathlib import Path

from memoryguard.rule_scope import infer_scope_from_text


ROOT = Path(__file__).resolve().parents[1]


def test_conflict_007_keeps_agent_and_project_identity_when_broad_signal_conflicts():
    payload = json.loads(
        (ROOT / "tests" / "golden" / "rule_scope_cases.json").read_text(
            encoding="utf-8",
        ),
    )
    case = next(item for item in payload["cases"] if item["case_id"] == "conflict-007")
    context = case["trusted_context"]

    result = infer_scope_from_text(
        case["text"],
        agent_instance_id=context["agent_instance_id"],
        project_ref=context["project_ref"],
    )

    assert result.selected.target_type == "agent_project"
    assert result.selected.target_id == context["agent_instance_id"]
    assert result.selected.project_ref == context["project_ref"]
    assert result.fallback_used is True
    assert any(candidate.target_type == "agent_project" for candidate in result.candidates)
