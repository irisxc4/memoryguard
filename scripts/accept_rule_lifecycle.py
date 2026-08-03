"""Deterministic acceptance check for the rule-scope lifecycle.

The script intentionally runs the production ``infer_scope_from_text``
function against the checked-in, human-labelled golden set and reports two
separate machine-readable families of metrics:

* ``intent_candidate_accuracy`` -- did the system recognise the *full* identity
  the labelled intent names (target type, target agent id and project)?
* ``safe_activation_accuracy``  -- of the cases it activated, was the activation
  never *wider* than the labelled intent (no silent permission widening)?

The two are distinct on purpose: a system that always falls back to the
current agent+project scores high on safety but low on intent recognition.
The script exits non-zero when either gate fails or any activation widens
scope or promotes to ``system``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memoryguard.rule_scope import infer_scope_from_text  # noqa: E402


DEFAULT_GOLDEN = ROOT / "tests" / "golden" / "rule_scope_cases.json"
MIN_CATEGORY_ACCURACY = 0.90

# Narrow-to-wide partial order for *safe activation* checks.  ``agent_project``
# (current agent + current project) is the narrowest audience this system can
# emit; ``agent`` (current agent, all its projects) is wider; provider/project/
# runtime_role/group/broad/system are progressively wider still.  The
# provider/project/runtime_role trio has no total order between its members,
# but each is strictly wider than ``agent`` for the purpose of "did we widen?".
WIDER_MAP: dict[str, set[str]] = {
    "agent_project": {"agent", "project", "provider", "runtime_role", "group", "broad", "system"},
    "agent": {"project", "provider", "runtime_role", "group", "broad", "system"},
    "project": {"provider", "runtime_role", "group", "broad", "system"},
    "provider": {"project", "runtime_role", "group", "broad", "system"},
    "runtime_role": {"project", "provider", "group", "broad", "system"},
    "group": {"broad", "system"},
    "broad": {"system"},
    "system": set(),
}


def _expected_identity(expected: dict[str, Any], context: dict[str, Any]) -> dict[str, str]:
    """Derive the full labelled identity for a target type.

    The golden set stores ``trusted_context`` (agent/project/provider) but the
    ``expected`` object only carries ``target_type``.  The target agent id and
    project ref follow deterministically from the trusted context, which is the
    same source ``infer_scope_from_text`` is asked to respect.
    """
    target_type = str(expected.get("target_type", "") or "")
    target_id = ""
    project_ref = ""
    if target_type in {"agent", "agent_project"}:
        target_id = str(context.get("agent_instance_id", "") or "")
    if target_type in {"agent_project", "project"}:
        project_ref = canonical_ref(str(context.get("project_ref", "") or ""))
    if target_type == "provider":
        target_id = str(context.get("provider", "") or "")
    return {"target_type": target_type, "target_id": target_id, "project_ref": project_ref}


def canonical_ref(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").rstrip("/")


def load_cases(path: str | Path = DEFAULT_GOLDEN) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = payload.get("cases", payload) if isinstance(payload, dict) else payload
    if not isinstance(cases, list):
        raise ValueError("golden set must contain a cases list")
    return cases


def evaluate_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(cases)
    intent_exact = 0
    activation_safe = 0
    over_broad = 0
    under_scoped = 0
    auto_system = 0
    fallback_count = 0
    errors: list[dict[str, Any]] = []
    categories = Counter()
    category_intent = Counter()
    category_safe = Counter()
    category_over_broad = Counter()
    production_defects: list[dict[str, Any]] = []

    for case in cases:
        case_id = str(case.get("case_id", ""))
        category = str(case.get("category", "uncategorized"))
        categories[category] += 1
        context = dict(case.get("trusted_context") or {})
        expected = dict(case.get("expected") or {})
        expected_effect = str(expected.get("effect", "include"))
        expected_fallback = bool(expected.get("fallback", expected.get("fallback_used", False)))
        expected_blocked = bool(expected.get("blocked", False))
        intent = _expected_identity(expected, context)
        actual: dict[str, Any]
        try:
            result = infer_scope_from_text(
                str(case.get("text", "")),
                agent_instance_id=str(context.get("agent_instance_id", "")),
                project_ref=str(context.get("project_ref", "")),
            )
            selected = result.selected
            actual = {
                "target_type": selected.target_type,
                "target_id": selected.target_id or "",
                "project_ref": canonical_ref(selected.project_ref or ""),
                "effect": "include",
                "fallback": bool(result.fallback_used),
                "blocked": False,
            }
        except Exception as exc:  # malformed trusted context is a blocked case
            actual = {"target_type": "", "target_id": "", "project_ref": "",
                      "effect": "include", "fallback": False, "blocked": True}
            errors.append({"case_id": case_id, "category": category, "error": str(exc)})

        if actual["target_type"] == "system":
            auto_system += 1
        if actual["fallback"]:
            fallback_count += 1

        # Intent recognition compares the FULL identity: type + target agent id
        # + project.  Type alone is not enough -- a case that resolves to the
        # wrong agent or project must not count as recognised.
        intent_ok = bool(
            intent["target_type"]
            and actual["target_type"] == intent["target_type"]
            and actual["target_id"] == intent["target_id"]
            and actual["project_ref"] == intent["project_ref"]
            and actual["blocked"] == expected_blocked
        )
        if intent_ok:
            intent_exact += 1
            category_intent[category] += 1

        # Safe activation: never *wider* than the labelled intent.  A narrower
        # activation is safe (counted as under-scoped); a wider one is the
        # failure this gate exists to catch.  Non-blocked fallbacks are allowed
        # as long as they do not widen.
        if expected_blocked:
            activation_ok = actual["blocked"]
        elif intent["target_type"] and actual["target_type"]:
            activation_ok = actual["target_type"] not in WIDER_MAP.get(
                intent["target_type"], set(),
            )
        else:
            activation_ok = False
        if activation_ok:
            activation_safe += 1
            category_safe[category] += 1
        else:
            if intent["target_type"] and actual["target_type"]:
                if (
                    actual["target_type"] in WIDER_MAP.get(intent["target_type"], set())
                    # A fallback activation explicitly asks the human to
                    # confirm; it never *silently* widens authority, so it is
                    # not an over-broad activation.
                    and not actual["fallback"]
                ):
                    over_broad += 1
                    category_over_broad[category] += 1
                elif actual["target_type"] != intent["target_type"]:
                    under_scoped += 1
            errors.append({
                "case_id": case_id,
                "category": category,
                "intent": intent,
                "actual": actual,
            })

        if case_id == "conflict-007" and (not intent_ok or not activation_ok):
            # This case is a known conflict-resolution safety witness.  Keep
            # the production result visible; never rewrite it into a pass.
            production_defects.append({
                "case_id": case_id,
                "category": category,
                "intent": intent,
                "actual": actual,
            })

    denominator = total or 1
    category_diff: dict[str, dict[str, Any]] = {}
    production_defect_categories = {
        str(item["category"]) for item in production_defects
    }
    for category in sorted(categories):
        category_total = categories[category]
        intent_accuracy = category_intent[category] / category_total
        safe_accuracy = category_safe[category] / category_total
        category_diff[category] = {
            "total": category_total,
            "intent": category_intent[category],
            "safe": category_safe[category],
            "intent_accuracy": intent_accuracy,
            "safe_accuracy": safe_accuracy,
            "over_broad": category_over_broad[category],
            "passed": bool(
                category_total > 0
                and intent_accuracy >= MIN_CATEGORY_ACCURACY
                and safe_accuracy >= MIN_CATEGORY_ACCURACY
                and category_over_broad[category] == 0
                and category not in production_defect_categories
            ),
        }
    categories_passed = bool(category_diff) and all(
        item["passed"] for item in category_diff.values()
    )
    report = {
        "total": total,
        "intent_candidate_accuracy": intent_exact / denominator,
        "safe_activation_accuracy": activation_safe / denominator,
        "over_broad_activation_rate": over_broad / denominator,
        "under_scoped_rate": under_scoped / denominator,
        "auto_system": auto_system,
        "auto_system_activation_rate": auto_system / denominator,
        "fallback_rate": fallback_count / denominator,
        "categories": dict(sorted(categories.items())),
        "category_diff": category_diff,
        "categories_passed": categories_passed,
        "production_defects": production_defects,
        "errors": errors[:25],
        "passed": bool(
            total >= 200
            and intent_exact / denominator >= MIN_CATEGORY_ACCURACY
            and activation_safe / denominator >= MIN_CATEGORY_ACCURACY
            and over_broad == 0
            and auto_system == 0
            and categories_passed
            and not production_defects
        ),
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", default=str(DEFAULT_GOLDEN), help="golden JSON path")
    args = parser.parse_args(argv)
    try:
        report = evaluate_cases(load_cases(args.golden))
    except Exception as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
