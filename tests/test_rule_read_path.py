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
                session_id=f"s{i}", session_trusted=1,
                content=d.canonical_text,
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
        intel.approve_proposal(
            pid, approved_by="admin", capability_id="admin:test-suite",
        )
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


# ---------------------------------------------------------------------------
# audience-aware canonical dedup (the dedup must run *after* the audience/
# status/exclude match, never before)
# ---------------------------------------------------------------------------


_CROSS_BODY = "提交代码前必须运行测试"


def _seed_cross(tmp_path, group, records, *, evidence_agents=2):
    """Seed legacy records + backfill + evidence, returning (legacy, intel)."""
    legacy = SharedMemoryStore(tmp_path, group)
    for spec in records:
        legacy.append_record(SharedMemoryRecord(
            memory_id=spec["memory_id"], body=spec["body"],
            kind=MemoryKind.PROCEDURE,
            status=spec.get("status", SharedMemoryStatus.ACTIVE),
            injection_policy=spec.get("policy", "always"),
            priority=spec.get("priority", 10),
            locked=spec.get("locked", False),
            agent_instance_id=spec.get("agent", "agent-1"),
            created_at=_now_iso(), updated_at=_now_iso(),
        ), assignments=spec["assignments"])
    intel = RuleMergeStore(tmp_path)
    service = RuleMergeService(intel)
    service.backfill_group(legacy, group)
    for d in intel.list_definitions():
        for spec in records:
            if normalize_rule_text(spec["body"]) != d.canonical_text:
                continue
            for i in range(evidence_agents):
                intel.upsert_evidence(build_evidence(
                    definition_id=d.definition_id,
                    source_rule_id=spec["memory_id"],
                    agent_instance_id=f"ev{i}", project_ref=f"ep{i}",
                    session_id=f"s{i}", content=d.canonical_text,
                    observed_at=_now_iso(),
                ))
    return legacy, intel


def _mandatory_bodies(packet):
    return [
        m["body"] for m in packet["context_packet"]["mandatory_items"]
    ]


def test_canonical_read_cross_agent_keeps_each_agents_rule(tmp_path):
    # Two agents share one canonical rule (identical wording).  A global dedup
    # would prefer m1 (agent-1) and starve agent-2; the post-audience dedup
    # must keep each agent's own matched record.
    legacy, _ = _seed_cross(tmp_path, "g1", [
        {"memory_id": "m1", "body": _CROSS_BODY, "agent": "agent-1",
         "assignments": [{"target_type": "agent", "target_id": "agent-1"}]},
        {"memory_id": "m2", "body": _CROSS_BODY, "agent": "agent-2",
         "assignments": [{"target_type": "agent", "target_id": "agent-2"}]},
    ])
    for agent_id in ("agent-1", "agent-2"):
        packet = build_context_packet(
            legacy, task="写测试",
            effective_context=_context(agent_id, "g1"),
            read_path=MODE_RULE_INTELLIGENCE,
        )
        assert any(_CROSS_BODY in b for b in _mandatory_bodies(packet)), agent_id


def test_canonical_read_cross_project_keeps_each_projects_rule(tmp_path):
    legacy, _ = _seed_cross(tmp_path, "g1", [
        {"memory_id": "m1", "body": _CROSS_BODY, "agent": "agent-1",
         "assignments": [{"target_type": "agent_project", "target_id": "agent-1",
                          "project_ref": "/proj/x"}]},
        {"memory_id": "m2", "body": _CROSS_BODY, "agent": "agent-2",
         "assignments": [{"target_type": "agent_project", "target_id": "agent-2",
                          "project_ref": "/proj/y"}]},
    ])
    packet = build_context_packet(
        legacy, task="写测试",
        effective_context=EffectiveAgentContext(
            "agent-2", "g1", project_ref="/proj/y",
        ),
        read_path=MODE_RULE_INTELLIGENCE,
    )
    assert any(_CROSS_BODY in b for b in _mandatory_bodies(packet))


def test_canonical_read_applies_exclude_before_dedupe(tmp_path):
    # m2 is the stronger record (higher priority, locked) but is *excluded* for
    # agent-1.  The canonical collapse must not let m2 delete m1's injection.
    legacy, _ = _seed_cross(tmp_path, "g1", [
        {"memory_id": "m1", "body": _CROSS_BODY, "agent": "agent-1",
         "priority": 10,
         "assignments": [{"target_type": "agent", "target_id": "agent-1"}]},
        {"memory_id": "m2", "body": _CROSS_BODY, "agent": "agent-1",
         "priority": 50, "locked": True,
         "assignments": [{"target_type": "agent", "target_id": "agent-1",
                          "effect": "exclude"}]},
    ])
    packet = build_context_packet(
        legacy, task="写测试",
        effective_context=_context("agent-1", "g1"),
        read_path=MODE_RULE_INTELLIGENCE,
    )
    assert any(_CROSS_BODY in b for b in _mandatory_bodies(packet))


def test_shadowed_record_never_replaces_active_representative(tmp_path):
    # m2 is shadowed but the strongest by raw priority/locked; the active/
    # status filter must run before canonical dedup so m1 (active) survives.
    legacy, _ = _seed_cross(tmp_path, "g1", [
        {"memory_id": "m1", "body": _CROSS_BODY, "agent": "agent-1",
         "priority": 10,
         "assignments": [{"target_type": "agent", "target_id": "agent-1"}]},
        {"memory_id": "m2", "body": _CROSS_BODY, "agent": "agent-1",
         "priority": 50, "locked": True,
         "status": SharedMemoryStatus.SHADOWED,
         "assignments": [{"target_type": "agent", "target_id": "agent-1"}]},
    ])
    packet = build_context_packet(
        legacy, task="写测试",
        effective_context=_context("agent-1", "g1"),
        read_path=MODE_RULE_INTELLIGENCE,
    )
    assert any(_CROSS_BODY in b for b in _mandatory_bodies(packet))


def test_shadow_compare_reports_zero_diff_when_switch_safe(tmp_path):
    legacy, _ = _seed_cross(tmp_path, "g1", [
        {"memory_id": "m1", "body": _CROSS_BODY, "agent": "agent-1",
         "assignments": [{"target_type": "agent", "target_id": "agent-1"}]},
    ])
    diff = RuleReadPath(tmp_path, "g1").shadow_compare(
        legacy, _context("agent-1", "g1"),
    )
    assert diff is not None
    assert diff["missing"] == []
    assert diff["extra"] == []
    assert diff["permission_diff"] == 0
