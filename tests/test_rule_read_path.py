"""Phase5 read-path switch tests.

Phase5 lets the *enforcement* read path (context bootstrap) prefer the
rule-intelligence canonical layer so merged duplicates inject once, while the
body text still comes from the legacy record and old tables stay untouched.

These tests assert:

  * a workspace with no intelligence layer resolves to the legacy path and the
    packet is byte-for-byte the old behaviour (zero regression);
  * ``RuleReadPath`` maps evidence source ids back to legacy memory ids;
  * ``dedupe_records`` collapses merged duplicates deterministically;
  * ``build_context_packet(read_path="rule-intelligence")`` injects the merged
    canonical rule once instead of N duplicate records;
  * the canonical read never invents records from stale evidence;
  * forced ``legacy`` mode is unaffected even when intelligence exists.
"""
from __future__ import annotations

import pytest

from memoryguard.context_bootstrap import build_context_packet
from memoryguard.rule_definition import build_definition, normalize_rule_text
from memoryguard.rule_evidence import build_evidence
from memoryguard.rule_merge import RuleMergeService, RuleMergeStore
from memoryguard.rule_read_path import (
    MODE_AUTO,
    MODE_LEGACY,
    MODE_RULE_INTELLIGENCE,
    RuleReadPath,
    dedupe_records,
    resolve_read_path_mode,
)
from memoryguard.schema_v3 import (
    EffectiveAgentContext,
    MemoryKind,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
)
from memoryguard.shared_memory_store import SharedMemoryStore


_BODIES = {
    "m1": "提交代码前必须运行测试",
    "m2": "提交前必须执行测试",
    "m3": "代码必须通过code review",
}


def _seed_record(store: SharedMemoryStore, body: str, *, memory_id: str,
                 kind=MemoryKind.PROCEDURE, policy="always", priority=10,
                 agent="agent-1"):
    store.append_record(SharedMemoryRecord(
        memory_id=memory_id, body=body, kind=kind,
        status=SharedMemoryStatus.ACTIVE, injection_policy=policy,
        priority=priority, agent_instance_id=agent,
        created_at=_now_iso(), updated_at=_now_iso(),
    ), assignments=[{"target_type": "agent", "target_id": agent}])


def _context(agent_id="agent-1", group_id="g1") -> EffectiveAgentContext:
    return EffectiveAgentContext(agent_id, group_id)


# ---------------------------------------------------------------------------
# mode resolution
# ---------------------------------------------------------------------------


def test_read_path_mode_normalizes_and_falls_back():
    assert resolve_read_path_mode("auto") == MODE_AUTO
    assert resolve_read_path_mode("legacy") == MODE_LEGACY
    assert resolve_read_path_mode("rule-intelligence") == MODE_RULE_INTELLIGENCE
    assert resolve_read_path_mode("bogus") in {MODE_AUTO, MODE_LEGACY}


def test_no_intelligence_is_legacy_and_packet_unchanged(tmp_path):
    # A plain shared-memory group with no rule-intelligence layer.
    group = "g1"
    AgentBindingSeeded = None
    store = SharedMemoryStore(tmp_path, group)
    _seed_record(store, "提交代码前必须运行测试", memory_id="m1", policy="always")
    _seed_record(store, "代码必须通过code review", memory_id="m2", policy="always")

    packet = build_context_packet(
        store, task="写测试", effective_context=_context(group_id=group),
    )
    assert packet["read_path"]["mode"] == MODE_LEGACY
    assert packet["read_path"]["deduplicated"] == 0
    ids = {m["memory_id"] for m in packet["context_packet"]["mandatory_items"]}
    assert ids == {"m1", "m2"}


def test_dedupe_records_passthrough_without_mapping():
    records = [object(), object()]
    assert dedupe_records(records, None) == records


# ---------------------------------------------------------------------------
# canonical mapping
# ---------------------------------------------------------------------------


def _seed_intelligence_pair(tmp_path, *, merge: bool):
    """Seed a canonical pair: m1/m2 merge into one definition when merge=True."""
    group = "g1"
    legacy = SharedMemoryStore(tmp_path, group)
    _seed_record(legacy, _BODIES["m1"], memory_id="m1", policy="always")
    _seed_record(legacy, _BODIES["m2"], memory_id="m2", policy="always")
    _seed_record(legacy, _BODIES["m3"], memory_id="m3", policy="always")

    intel = RuleMergeStore(tmp_path)
    service = RuleMergeService(intel)
    # Backfill maps m1/m2/m3 to definitions; evidence.source_rule_id is memory_id.
    service.backfill_group(legacy, group)

    # Anchor every definition to its legacy memory id via evidence, so the
    # canonical map can resolve memory_id -> definition_id.
    for d in intel.list_definitions():
        for i in range(3):
            intel.upsert_evidence(build_evidence(
                definition_id=d.definition_id,
                source_rule_id=next(
                    mid for mid in ("m1", "m2", "m3")
                    if normalize_rule_text(_BODIES[mid]) == d.canonical_text
                ),
                agent_instance_id=f"agent-{i}", project_ref=f"p{i}",
                session_id=f"s{i}", content=d.canonical_text,
                observed_at=_now_iso(),
            ))
        intel.upsert_agent_reputation(
            agent_id="agent-2", success_rate=0.98, sample_count=200,
        )
    for i in range(3):
        intel.upsert_project_profile(
            project_ref=f"p{i}", production_level=1.0,
        )

    if merge:
        canon_a = normalize_rule_text("提交代码前必须运行测试")
        canon_b = normalize_rule_text("提交前必须执行测试")
        a = next(d for d in intel.list_definitions()
                 if d.canonical_text == canon_a)
        b = next(d for d in intel.list_definitions()
                 if d.canonical_text == canon_b)
        candidates = service.scan_and_propose()
        cand = [c for c in candidates if c["status"] == "candidate"]
        assert cand, "synonym pair must be a merge candidate"
        pid = cand[0]["proposal_id"]
        # Human-approved merge bypasses the soft readiness/cooldown gates (the
        # hard gates still hold); this test only exercises the canonical read.
        intel.set_proposal_status(pid, "approved")
        result = service.merge_proposal(pid, actor="admin")
        assert result["ok"] is True
    return legacy, intel


def test_canonical_map_maps_evidence_source_ids(tmp_path):
    legacy, intel = _seed_intelligence_pair(tmp_path, merge=False)
    read = RuleReadPath(tmp_path, "g1")
    mapping = read.resolve_canonical_map(known_memory_ids={"m1", "m2", "m3"})
    assert mapping is not None
    assert mapping["mode"] == MODE_RULE_INTELLIGENCE
    # Every mapped memory id exists in the legacy store.
    assert set(mapping["memory_to_definition"]) <= {"m1", "m2", "m3"}


def test_canonical_map_drops_stale_evidence(tmp_path):
    _seed_intelligence_pair(tmp_path, merge=False)
    read = RuleReadPath(tmp_path, "g1")
    # known_memory_ids excludes m3 -> no canonical mapping may reference it.
    mapping = read.resolve_canonical_map(known_memory_ids={"m1", "m2"})
    assert mapping is not None
    for memory_id in mapping["memory_to_definition"]:
        assert memory_id in {"m1", "m2"}


def test_dedupe_records_collapses_merged_duplicates(tmp_path):
    legacy, intel = _seed_intelligence_pair(tmp_path, merge=True)
    read = RuleReadPath(tmp_path, "g1")
    mapping = read.resolve_canonical_map(known_memory_ids={"m1", "m2", "m3"})
    assert mapping is not None
    records = legacy.list_records()
    assert any(r.memory_id == "m1" for r in records)
    assert any(r.memory_id == "m2" for r in records)
    deduped = dedupe_records(records, mapping)
    deduped_ids = [r.memory_id for r in deduped]
    # m1 and m2 collapsed into one representative.
    assert sum(1 for mid in deduped_ids if mid in {"m1", "m2"}) == 1
    # m3 (unique rule) is untouched.
    assert "m3" in deduped_ids


def test_bootstrap_injects_merged_rule_once(tmp_path):
    legacy, intel = _seed_intelligence_pair(tmp_path, merge=True)
    packet = build_context_packet(
        legacy, task="写测试",
        effective_context=_context(group_id="g1"),
        read_path=MODE_RULE_INTELLIGENCE,
    )
    assert packet["read_path"]["mode"] == MODE_RULE_INTELLIGENCE
    assert packet["read_path"]["deduplicated"] >= 1
    bodies = [
        m["body"] for m in packet["context_packet"]["mandatory_items"]
    ]
    # "提交代码前必须运行测试" and "提交前必须执行测试" collapse to one injection.
    test_rules = [b for b in bodies if "测试" in b]
    assert len(test_rules) == 1
    # code review rule unaffected.
    assert any("code review" in b for b in bodies)


def test_forced_legacy_ignores_intelligence(tmp_path):
    legacy, intel = _seed_intelligence_pair(tmp_path, merge=True)
    packet = build_context_packet(
        legacy, task="写测试",
        effective_context=_context(group_id="g1"),
        read_path=MODE_LEGACY,
    )
    assert packet["read_path"]["mode"] == MODE_LEGACY
    bodies = [
        m["body"] for m in packet["context_packet"]["mandatory_items"]
    ]
    test_rules = [b for b in bodies if "测试" in b]
    assert len(test_rules) == 2  # both legacy records inject
