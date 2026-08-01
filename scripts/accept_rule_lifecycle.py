"""Deterministic acceptance check for the rule-scope lifecycle.

The script intentionally runs the production ``infer_scope_from_text``
function against the checked-in, human-labelled golden set.  It reports
machine-readable metrics and exits non-zero when the safety gates fail.
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
SCOPE_RANK = {
    "agent": 1,
    "agent_project": 2,
    "runtime_role": 2,
    "project": 3,
    "group": 3,
    "provider": 3,
    "broad": 4,
    "system": 5,
}


def load_cases(path: str | Path = DEFAULT_GOLDEN) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = payload.get("cases", payload) if isinstance(payload, dict) else payload
    if not isinstance(cases, list):
        raise ValueError("golden set must contain a cases list")
    return cases


def evaluate_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(cases)
    exact = 0
    fallback_count = 0
    under_scoped = 0
    over_scoped = 0
    auto_system = 0
    errors: list[dict[str, Any]] = []
    categories = Counter()
    category_exact = Counter()

    for case in cases:
        case_id = str(case.get("case_id", ""))
        category = str(case.get("category", "uncategorized"))
        categories[category] += 1
        context = dict(case.get("trusted_context") or {})
        expected = dict(case.get("expected") or {})
        expected_target = str(expected.get("target_type", ""))
        expected_effect = str(expected.get("effect", "include"))
        expected_fallback = bool(expected.get("fallback", expected.get("fallback_used", False)))
        expected_blocked = bool(expected.get("blocked", False))
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
                "target_id": selected.target_id,
                "project_ref": selected.project_ref,
                "effect": "include",
                "fallback": bool(result.fallback_used),
                "blocked": False,
            }
        except Exception as exc:  # malformed trusted context is a blocked case
            actual = {"target_type": "", "effect": "include", "fallback": False, "blocked": True}
            errors.append({"case_id": case_id, "category": category, "error": str(exc)})

        if actual["target_type"] == "system":
            auto_system += 1
        if actual["fallback"]:
            fallback_count += 1
        expected_rank = SCOPE_RANK.get(expected_target, 0)
        actual_rank = SCOPE_RANK.get(actual["target_type"], 0)
        if actual_rank < expected_rank:
            under_scoped += 1
        elif actual_rank > expected_rank:
            over_scoped += 1
        is_exact = (
            actual["target_type"] == expected_target
            and actual["effect"] == expected_effect
            and actual["fallback"] == expected_fallback
            and actual["blocked"] == expected_blocked
        )
        if is_exact:
            exact += 1
            category_exact[category] += 1
        elif len(errors) == 0 or errors[-1].get("case_id") != case_id:
            errors.append({
                "case_id": case_id,
                "category": category,
                "expected": {
                    "target_type": expected_target,
                    "effect": expected_effect,
                    "fallback": expected_fallback,
                    "blocked": expected_blocked,
                },
                "actual": actual,
            })

    denominator = total or 1
    accuracy = exact / denominator
    report = {
        "total": total,
        "exact": exact,
        "golden_scope_accuracy": accuracy,
        "fallback_rate": fallback_count / denominator,
        "under_scoped_rate": under_scoped / denominator,
        "over_scoped_rate": over_scoped / denominator,
        "auto_system": auto_system,
        "categories": dict(sorted(categories.items())),
        "category_diff": {
            category: {
                "total": categories[category],
                "exact": category_exact[category],
                "accuracy": category_exact[category] / categories[category],
            }
            for category in sorted(categories)
        },
        "errors": errors[:25],
        "passed": bool(total >= 200 and accuracy >= 0.90 and auto_system == 0),
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
