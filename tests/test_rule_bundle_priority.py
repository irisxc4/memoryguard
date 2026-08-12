"""V2 rule-audience bundle invariants.

The canonical V2 rules plane stores each definition and binding explicitly.
Priority is an attribute of the binding, not part of its audience identity;
these tests keep that invariant at the public ``RuleV2Store`` boundary.
"""
from __future__ import annotations

from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store


AUDIENCE = {"target_type": "agent", "target_id": "codex"}


def _rule(store: RuleV2Store, memory_id: str, priority: int, audience: dict = AUDIENCE):
    definition = store.upsert_definition(build_definition(
        f"Codex 规则 {memory_id}", kind="procedure", rule_strength="must",
    ))
    binding = build_binding(
        definition.definition_id,
        share_group_id="g1",
        target_type=audience["target_type"],
        target_id=audience.get("target_id", ""),
        priority=priority,
        owner_agent_id="codex",
        created_by="admin",
        authorization="test",
        binding_id=memory_id,
    )
    store.upsert_binding(binding)
    return definition, binding


def _audience_key(binding):
    return (
        binding.share_group_id,
        binding.target_type,
        binding.target_id,
        binding.project_ref,
        binding.provider,
        binding.runtime_role,
        binding.effect,
    )


def test_priority_does_not_split_bundle(tmp_path):
    store = RuleV2Store(tmp_path)
    for memory_id, priority in (("m1", 100), ("m2", 20), ("m3", 10)):
        _rule(store, memory_id, priority)
    bindings = store.list_bindings(share_group_id="g1", status="active")
    grouped = {}
    for binding in bindings:
        grouped.setdefault(_audience_key(binding), []).append(binding)
    assert len(grouped) == 1
    assert max(item.priority for item in next(iter(grouped.values()))) == 100


def test_same_priority_also_one_bundle(tmp_path):
    store = RuleV2Store(tmp_path)
    _rule(store, "m1", 50)
    _rule(store, "m2", 50)
    bindings = store.list_bindings(share_group_id="g1", status="active")
    assert len({_audience_key(item) for item in bindings}) == 1
    assert {item.priority for item in bindings} == {50}


def test_different_audience_stays_separate(tmp_path):
    store = RuleV2Store(tmp_path)
    _rule(store, "m1", 100, {"target_type": "agent", "target_id": "codex"})
    _rule(store, "m2", 10, {"target_type": "agent", "target_id": "cursor"})
    bindings = store.list_bindings(share_group_id="g1", status="active")
    assert len({_audience_key(item) for item in bindings}) == 2
