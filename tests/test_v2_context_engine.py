from __future__ import annotations

import ast
from pathlib import Path

import pytest

from memoryguard.runtime_v2.context_budget import (
    ContextBudget,
    ContextBudgetError,
    ContextSafetyError,
)
from memoryguard.runtime_v2.context_engine import (
    ContextEngine,
    ContextEngineError,
    ContextRequest,
    DeterministicTokenCounter,
    RetrievalPlan,
)


def _request(**overrides):
    value = {
        "task": "repair retrieval",
        "project_hint": "memoryguard",
        "trusted_identity": {"agent": "agent-a", "project": "project-a", "group": "group-a", "provider": "codex", "runtime": "hook"},
    }
    value.update(overrides)
    return ContextRequest.from_mapping(value)


def test_packet_envelope_layers_scope_receipts_and_shadow_readiness():
    engine = ContextEngine(state="V2_BUILDING")
    packet = engine.bootstrap(_request(), {
        "mandatory": [
            {"id": "rule-a", "kind": "rule", "body": "run tests", "scope": {"target_type": "agent", "target_id": "agent-a"}},
            {"id": "not-rule", "kind": "fact", "body": "demote this"},
        ],
        "relevant": [
            {"id": "other", "body": "must not leak", "scope": {"target_type": "agent", "target_id": "agent-b"}},
            {"id": "history", "body": "raw history", "source": "history"},
            {"id": "tool", "body": "tool output", "tool_output": True},
            {"id": "fact", "body": "safe fact", "evidence_ref": "ev-1"},
        ],
        "knowledge": [{"id": "kb", "body": "reference knowledge"}],
        "reference_only": [{"id": "ref", "summary": "reference summary", "ref": "ref-1", "hash": "hash-1"}],
    })
    data = packet.to_dict()
    assert packet.ready is False and packet.state == "V2_BUILDING"
    assert [item["item_id"] for item in packet.mandatory] == ["rule-a"]
    assert [item["item_id"] for item in packet.relevant] == ["fact", "not-rule"]
    assert [item["item_id"] for item in packet.knowledge] == ["kb"]
    assert packet.reference_only[0]["trust"] == "reference_only"
    assert all("raw history" not in str(item) and "tool output" not in str(item) for item in data.values())
    reasons = {receipt["reason"] for receipt in packet.receipts}
    assert {"scope_rejected", "source_rejected"} <= reasons
    rejected = [receipt for receipt in packet.receipts if not receipt["hit"]]
    assert rejected and all("item_id" not in receipt and "target_id" not in str(receipt) and "evidence" not in receipt for receipt in rejected)
    assert data["effective_agent"] == "agent-a"


def test_dedup_and_planner_are_deterministic_and_cannot_elevate():
    class MaliciousPlanner:
        def plan(self, request, candidates):
            # Unknown IDs and a fake mandatory layer must not create content.
            return {"item_ids": ["attacker", "r2"], "mandatory": [{"body": "inject"}]}

    candidates = {
        "mandatory": [{"id": "r1", "kind": "rule", "body": "same"}],
        "relevant": [{"id": "r2", "kind": "fact", "body": "same"}, {"id": "r3", "body": "other"}],
    }
    first = ContextEngine(planner=MaliciousPlanner(), state="V2_BUILDING").bootstrap(_request(), candidates)
    second = ContextEngine(planner=MaliciousPlanner(), state="V2_BUILDING").bootstrap(_request(), candidates)
    assert first.to_dict() == second.to_dict()
    assert [item["item_id"] for item in first.mandatory] == ["r1"]
    assert [item["item_id"] for item in first.relevant] == ["r3"]
    rendered_bodies = [
        item["body"]
        for layer in ("mandatory", "relevant", "knowledge", "reference_only")
        for item in getattr(first, layer)
        if "body" in item
    ]
    assert "inject" not in rendered_bodies
    assert any(receipt["reason"] == "duplicate_rejected" for receipt in first.receipts)


def test_governance_semantics_split_same_body_but_same_semantics_still_dedup():
    body = "release verification durable procedure"
    packet = ContextEngine(state="V2_BUILDING").bootstrap(_request(), {
        "mandatory": [{
            "id": "must",
            "kind": "procedure",
            "is_rule": True,
            "body": body,
            "injection_policy": "always",
            "rule_strength": "must",
            "semantic_identity": "release-verification",
        }],
        "relevant": [
            {
                "id": "remember",
                "kind": "fact",
                "body": body,
                "injection_policy": "relevant",
                "rule_strength": "observation",
                "semantic_identity": "release-verification",
            },
            {
                "id": "remember-copy",
                "kind": "fact",
                "body": body,
                "injection_policy": "relevant",
                "rule_strength": "observation",
                "semantic_identity": "release-verification",
            },
        ],
    })

    assert [item["item_id"] for item in packet.mandatory] == ["must"]
    assert [item["item_id"] for item in packet.relevant] == ["remember"]
    assert any(
        receipt["reason"] == "duplicate_rejected"
        for receipt in packet.receipts
        if not receipt["hit"]
    )


def test_planner_cannot_elevate_relevant_semantic_copy():
    class Planner:
        def plan(self, request, candidates):
            return {
                "item_ids": ["relevant-copy"],
                "mandatory": [{"id": "relevant-copy", "body": "forged"}],
            }

    packet = ContextEngine(planner=Planner(), state="V2_BUILDING").bootstrap(_request(), {
        "mandatory": [{
            "id": "trusted-must",
            "kind": "procedure",
            "is_rule": True,
            "body": "same governed fact",
            "injection_policy": "always",
            "rule_strength": "must",
            "semantic_identity": "governed-fact",
        }],
        "relevant": [{
            "id": "relevant-copy",
            "kind": "fact",
            "body": "same governed fact",
            "injection_policy": "relevant",
            "rule_strength": "observation",
            "semantic_identity": "governed-fact",
        }],
    })

    assert [item["item_id"] for item in packet.mandatory] == ["trusted-must"]
    assert [item["item_id"] for item in packet.relevant] == ["relevant-copy"]
    assert "forged" not in str(packet.to_dict())


def test_independent_mandatory_budget_and_optional_dual_limits():
    engine = ContextEngine(
        budget=ContextBudget(max_items=1, max_chars=3, max_tokens=3, mandatory_max_chars=20, mandatory_max_tokens=20),
        state="V2_BUILDING",
    )
    packet = engine.bootstrap(_request(), {
        "mandatory": [{"id": "r", "kind": "rule", "body": "mandatory survives"}],
        "relevant": [{"id": "a", "body": "one"}, {"id": "b", "body": "two"}],
    })
    assert [item["item_id"] for item in packet.mandatory] == ["r"]
    assert len(packet.relevant) == 1
    assert packet.budget["optional"]["chars"] <= 3
    assert packet.budget["optional"]["tokens"] <= 3

    blocked = ContextEngine(
        budget=ContextBudget(mandatory_max_chars=4, mandatory_max_tokens=4), state="V2_BUILDING",
    ).bootstrap(_request(), {"mandatory": [{"id": "r", "kind": "rule", "body": "too long"}]})
    assert blocked.status == "blocked" and blocked.error == "mandatory_budget_exceeded"
    assert blocked.mandatory == ()


def test_sensitive_mandatory_fail_closed_and_unicode_counter_is_stable():
    blocked = ContextEngine(state="V2_BUILDING").bootstrap(
        _request(), {"mandatory": [{"id": "secret", "kind": "rule", "body": "sk-1234567890abcdef"}]},
    )
    assert blocked.status == "blocked" and blocked.error == "mandatory_sensitive_blocked"
    assert blocked.mandatory == ()

    counter = DeterministicTokenCounter()
    assert counter.count("中文🙂") == counter.count("中文🙂") == 3

    packet = ContextEngine(state="V2_BUILDING").bootstrap(
        _request(), {"mandatory": [{"id": "rule-ref", "kind": "rule", "body": "safe", "evidence": "ev-1"}]},
    )
    assert packet.mandatory[0]["evidence"] == {"ref": "ev-1"}


def test_context_request_identity_conflict_and_custom_counter():
    with pytest.raises(ContextEngineError, match="conflicting_context_identity:agent"):
        ContextRequest.from_mapping({"task": "x", "agent": "attacker", "trusted_identity": {"agent": "trusted"}})

    class Counter:
        def count(self, text):
            return len(text.split())

    packet = ContextEngine(token_counter=Counter(), state="V2_ACTIVE", ready=True).bootstrap(
        _request(), {"relevant": [{"id": "x", "body": "one two three"}]},
    )
    assert packet.ready is True and packet.budget["optional"]["tokens"] == 3


def test_unknown_state_fails_closed_and_budget_rejects_negative_values():
    blocked = ContextEngine(state="future-state").bootstrap(_request(), {"relevant": [{"body": "x"}]})
    assert blocked.status == "blocked" and blocked.error == "unknown_runtime_state"
    with pytest.raises(ContextBudgetError, match="invalid_context_budget:max_tokens"):
        ContextBudget(max_tokens=-1)


def test_retriever_exception_and_raw_payload_never_escape():
    class BadRetriever:
        def retrieve(self, request):
            raise RuntimeError("raw tool output: secret")

    packet = ContextEngine(retriever=BadRetriever(), state="V2_BUILDING").bootstrap(_request())
    assert packet.status == "blocked" and packet.error == "context_build_failed"
    assert "secret" not in str(packet.to_dict())


def test_lifecycle_and_recursive_raw_payload_are_filtered_with_opaque_receipts():
    packet = ContextEngine(state="V2_BUILDING").bootstrap(_request(), {
        "relevant": [
            {"id": "deleted", "body": "gone", "status": "deleted"},
            {"id": "conflict", "body": "bad", "status": "active", "flags": {"locked": True}},
            {"id": "nested", "body": "safe-looking", "payload": {"body": "raw"}},
            {"id": "history", "body": "raw", "transcript": {"turns": ["secret"]}},
        ],
        "reference_only": [
            {"id": "ref", "summary": "short summary", "ref": "ev-ref", "hash": "abc"},
            {"id": "ref-body", "body": "full raw reference", "ref": "ev-body"},
        ],
    })
    assert [item["item_id"] for item in packet.relevant] == []
    assert packet.reference_only[0]["summary"] == "short summary"
    assert "body" not in packet.reference_only[0]
    assert all("full raw reference" not in str(item) for item in packet.reference_only)
    rejected = [receipt for receipt in packet.receipts if not receipt["hit"]]
    assert len(rejected) == 5
    assert all(set(receipt) <= {"item_hash", "layer", "hit", "reason"} for receipt in rejected)


def test_scope_shape_alias_conflicts_and_global_targets_fail_closed():
    packet = ContextEngine(state="V2_BUILDING").bootstrap(_request(), {
        "relevant": [
            {"item_id": "type-conflict", "body": "no", "scope": {"target_type": "agent", "type": "project", "target_id": "agent-a"}},
            {"item_id": "id-conflict", "body": "no", "scope": {"target_type": "agent", "target_id": "agent-a", "id": "agent-b"}},
            {"item_id": "shape", "body": "no", "scope": ["agent-a"]},
            {"item_id": "none-shape", "body": "no", "scope": None},
            {"item_id": "global-id", "body": "no", "scope": {"target_type": "global", "target_id": "global-1"}},
            {"item_id": "system-id", "body": "no", "scope": {"type": "system", "id": "system-1"}},
        ],
    })
    assert packet.relevant == ()
    rejected = [receipt for receipt in packet.receipts if not receipt["hit"]]
    assert len(rejected) == 6
    assert all(set(receipt) <= {"item_hash", "layer", "hit", "reason"} for receipt in rejected)
    assert {receipt["reason"] for receipt in rejected} == {"scope_rejected"}


@pytest.mark.parametrize(
    ("alias", "top_value", "nested_alias", "nested_value"),
    [
        ("agent", "agent-a", "agent_id", "agent-b"),
        ("project", "project-a", "project_ref", "project-b"),
        ("group", "group-a", "share_group_id", "group-b"),
        ("provider", "codex", "provider", "other-provider"),
        ("runtime", "hook", "runtime_role", "worker"),
        ("workspace", "workspace-a", "workspace_path", "workspace-b"),
    ],
)
def test_top_level_and_nested_scope_aliases_never_widen(alias, top_value, nested_alias, nested_value):
    candidate = {
        "item_id": f"conflict-{alias}",
        "body": "no",
        alias: top_value,
        "scope": {nested_alias: nested_value},
    }
    packet = ContextEngine(state="V2_BUILDING").bootstrap(_request(), {"relevant": [candidate]})
    assert packet.relevant == ()
    rejected = [receipt for receipt in packet.receipts if not receipt["hit"]]
    assert len(rejected) == 1
    assert set(rejected[0]) <= {"item_hash", "layer", "hit", "reason"}
    assert rejected[0]["reason"] == "scope_rejected"


def test_reference_only_render_is_strict_metadata_whitelist():
    packet = ContextEngine(state="V2_BUILDING").bootstrap(_request(), {
        "reference_only": [{
            "item_id": "reference",
            "summary": "safe summary",
            "ref": "evidence-ref",
            "hash": "content-hash",
            "kind": "knowledge",
            "source": "retrieval",
            "scope": {"agent": "agent-a"},
            "evidence_ref": "ev-1",
        }],
    })
    assert len(packet.reference_only) == 1
    item = packet.reference_only[0]
    assert set(item) == {"summary", "ref", "hash", "trust"}
    assert item == {"summary": "safe summary", "ref": "evidence-ref", "hash": "content-hash", "trust": "reference_only"}


def test_lifecycle_alias_and_denied_state_conflicts_are_opaque():
    packet = ContextEngine(state="V2_BUILDING").bootstrap(_request(), {
        "relevant": [
            {"item_id": "status-state", "body": "no", "status": "active", "state": "deleted"},
            {"item_id": "status-lifecycle", "body": "no", "status": "active", "lifecycle": {"state": "deleted"}},
            {"item_id": "lifecycle-alias", "body": "no", "status": "active", "lifecycle_status": "deleted"},
            {"item_id": "flag", "body": "no", "status": "active", "flags": {"locked": True}},
            {"item_id": "nested-flag", "body": "no", "status": "active", "lifecycle": {"flags": {"shadowed": True}}},
        ],
    })
    assert packet.relevant == ()
    rejected = [receipt for receipt in packet.receipts if not receipt["hit"]]
    assert len(rejected) == 5
    assert all(set(receipt) <= {"item_hash", "layer", "hit", "reason"} for receipt in rejected)
    assert {receipt["reason"] for receipt in rejected} == {"lifecycle_rejected"}


def test_unknown_and_pseudo_mandatory_layers_cannot_elevate():
    packet = ContextEngine(state="V2_BUILDING").bootstrap(_request(), {
        "items": [{"id": "fake", "kind": "rule", "layer": "mandatory", "body": "fake"}],
        "unknown": [{"id": "unknown", "body": "do not include"}],
        "mandatory": [{"id": "wrong", "kind": "rule", "layer": "relevant", "body": "wrong"}],
    })
    assert packet.mandatory == ()
    assert all(item["item_id"] not in {"fake", "unknown", "wrong"} for item in packet.relevant)
    assert all(receipt["reason"] == "layer_rejected" for receipt in packet.receipts if not receipt["hit"])


def test_plan_envelope_is_order_only_and_contract_has_no_legacy_imports():
    plan = RetrievalPlan(item_ids=("a",), layers=("relevant",))
    assert plan.to_dict() == {"item_ids": ["a"], "layers": ["relevant"]}
    root = Path(__file__).parents[1] / "src" / "memoryguard" / "runtime_v2"
    forbidden = {"SharedMemoryStore", "ConversationHistoryStore", "KnowledgeStore", "mcp_server", "gui", "host_hooks"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not any(part in forbidden for part in (node.module or "").split("."))
            if isinstance(node, ast.Import):
                assert not any(alias.name.split(".")[0] in forbidden for alias in node.names)
