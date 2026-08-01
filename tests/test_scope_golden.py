"""Golden-set acceptance for deterministic automatic rule scope inference."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memoryguard.rule_scope import infer_scope_from_text  # noqa: E402


GOLDEN = ROOT / "tests" / "golden" / "rule_scope_cases.json"
REQUIRED_CATEGORIES = {
    "project", "current_agent", "all_agents", "subagent", "provider",
    "negative_scope", "conflict", "bilingual", "no_signal", "multi_signal",
}


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    payload = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return payload["cases"]


def test_scope_golden_has_two_hundred_unique_manual_cases(cases):
    assert len(cases) >= 200
    assert len({case["case_id"] for case in cases}) == len(cases)
    assert len({case["text"] for case in cases}) == len(cases)
    assert {case["category"] for case in cases} == REQUIRED_CATEGORIES
    for case in cases:
        assert case["text"].strip()
        assert set(("text", "trusted_context", "expected", "case_id", "category")) <= set(case)
        assert case["trusted_context"]["agent_instance_id"]
        assert "target_type" in case["expected"] or case["expected"].get("blocked")


def _expected_identity(expected, context):
    """Derive the full labelled identity (type + agent id + project)."""
    target_type = expected.get("target_type", "")
    target_id = ""
    project_ref = ""
    if target_type in {"agent", "agent_project"}:
        target_id = context["agent_instance_id"]
    if target_type in {"agent_project", "project"}:
        project_ref = str(context.get("project_ref", "") or "")
    if target_type == "provider":
        target_id = str(context.get("provider", "") or "")
    return target_type, target_id, project_ref


def test_scope_golden_accuracy_and_no_system_promotion(cases):
    exact = 0
    for case in cases:
        context = case["trusted_context"]
        expected = case["expected"]
        result = infer_scope_from_text(
            case["text"],
            agent_instance_id=context["agent_instance_id"],
            project_ref=context.get("project_ref", ""),
        )
        selected = result.selected
        assert selected.target_type != "system", case["case_id"]
        # Compare the FULL identity, not just the target type: resolving to the
        # wrong agent or the wrong project must not count as correct.
        exp_type, exp_target_id, exp_project_ref = _expected_identity(expected, context)
        if (
            selected.target_type == exp_type
            and (selected.target_id or "") == exp_target_id
            and (selected.project_ref or "") == exp_project_ref
            and "include" == expected.get("effect", "include")
            and bool(result.fallback_used) == bool(expected.get("fallback", False))
        ):
            exact += 1
    accuracy = exact / len(cases)
    assert 0.0 <= accuracy <= 1.0
    assert accuracy >= 0.90


def test_scope_golden_outputs_bounded_metrics(cases):
    fallback_rate = sum(
        bool(infer_scope_from_text(
            case["text"],
            agent_instance_id=case["trusted_context"]["agent_instance_id"],
            project_ref=case["trusted_context"].get("project_ref", ""),
        ).fallback_used)
        for case in cases
    ) / len(cases)
    assert 0.0 <= fallback_rate <= 1.0
