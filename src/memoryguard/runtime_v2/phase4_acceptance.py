"""Deterministic Phase-4 runtime-equivalence acceptance evidence.

This is intentionally synthetic: it proves ContextEngine's filtering/budget/
scope semantics independently from any production V1/V2 database.  Readiness
can call the same function directly instead of requiring a human-generated
intermediate JSON file.
"""
from __future__ import annotations

from typing import Any

from .context_engine import ContextEngine, ContextRequest


def phase4_acceptance_evidence() -> dict[str, Any]:
    request = ContextRequest.from_mapping({
        "task": "phase4 acceptance",
        "project_hint": "fixture",
        "trusted_identity": {
            "agent": "agent-a", "project": "project-a", "group": "group-a",
            "provider": "codex", "runtime": "hook",
        },
    })
    fixture = {
        "mandatory": [
            {"id": "rule-a", "kind": "rule", "body": "run deterministic tests", "scope": {"target_type": "agent", "target_id": "agent-a"}},
        ],
        "relevant": [
            {"id": "fact-a", "kind": "fact", "body": "use the fixture project"},
            {"id": "history-a", "kind": "fact", "body": "raw history should be excluded", "source": "history"},
            {"id": "other-agent", "kind": "fact", "body": "scope leak", "scope": {"target_type": "agent", "target_id": "agent-b"}},
            {"id": "dup", "kind": "fact", "body": "use the fixture project", "priority": -1},
        ],
    }
    v1_ids = ["fact-a", "history-a", "other-agent", "dup"]
    v1_tokens = sum(len(item["body"]) for values in fixture.values() for item in values)
    engine = ContextEngine(state="V2_BUILDING", ready=False)
    first = engine.bootstrap(request, fixture).to_dict()
    second = engine.bootstrap(request, fixture).to_dict()
    v2_items = first["mandatory"] + first["relevant"] + first["knowledge"] + first["reference_only"]
    v2_ids = [item["item_id"] for item in v2_items]
    v2_tokens = int(first["budget"].get("total_tokens", 0))
    mandatory_v1 = [item["id"] for item in fixture["mandatory"]]
    mandatory_v2 = [item["item_id"] for item in first["mandatory"]]
    scope_leaks = [
        item["item_id"]
        for item in v2_items
        if item.get("scope", {}).get("target_type") == "agent"
        and item.get("scope", {}).get("target_id") != request.effective_agent
    ]
    v1_recall = {"fact-a"}
    v2_recall = set(v2_ids)
    runtime_leaks = [item_id for item_id in v2_ids if item_id == "history-a"]
    ok = (
        mandatory_v1 == mandatory_v2
        and not scope_leaks
        and not runtime_leaks
        and len(v2_recall & v1_recall) >= len(v1_recall)
        and v2_tokens < v1_tokens
        and first == second
        and first.get("ready") is False
        and first.get("state") == "V2_BUILDING"
    )
    return {
        "ok": ok,
        "status": "PASS" if ok else "BLOCKED",
        "state": first.get("state"),
        "ready": first.get("ready"),
        "mandatory_equivalence": mandatory_v1 == mandatory_v2,
        "scope_leak_count": len(scope_leaks),
        "scope_leaks": scope_leaks,
        "leak": len(runtime_leaks),
        "runtime_leaks": runtime_leaks,
        "recall_at_k": len(v2_recall & v1_recall),
        "v1_recall_at_k": len(v1_recall),
        "recall_ids": {"v1": sorted(v1_recall), "v2": sorted(v2_recall & v1_recall)},
        "context_tokens": {"v1": v1_tokens, "v2": v2_tokens},
        "deterministic": first == second,
        "v1_ids": v1_ids,
        "v2_ids": v2_ids,
    }


__all__ = ["phase4_acceptance_evidence"]
