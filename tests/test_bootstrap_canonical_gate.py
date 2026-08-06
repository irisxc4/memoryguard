"""Req8: bootstrap read-path gating + the 5 new output fields.

``build_context_packet`` must NEVER switch a group to the canonical read path
just because the caller passed ``read_path="auto"`` /
``read_path="rule-intelligence"``.  The canonical layer only engages when the
group-level canonical activation is persisted (``rule_canonical_state``
``activation_status == "active"``) *and*
``canonical_reconciliation_status`` reports ``canonical_ready``.  Every other
outcome stays on legacy with ``fallback_reason`` recording why.

The packet additionally carries: ``requested_read_path`` (raw caller value),
``effective_read_path``, ``fallback_reason``, ``canonical_definitions`` and
``canonical_ready``.
"""
from __future__ import annotations

import json as _json

from memoryguard.context_bootstrap import build_context_packet
from memoryguard.governance_scope import (
    GovernanceScope,
    build_shared_memory_graph,
    share_group_projection_path,
)
from memoryguard.rule_definition import normalize_rule_text
from memoryguard.rule_evidence import build_evidence
from memoryguard.rule_merge import RuleMergeService, RuleMergeStore
from memoryguard.rule_read_path import MODE_LEGACY, MODE_RULE_INTELLIGENCE
from memoryguard.rule_reconciliation import RuleReconciliationStore
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


def _seed_group(tmp_path, group="g1"):
    """Seed 3 governed records, backfill the P3 layer, return (legacy, intel)."""
    legacy = SharedMemoryStore(tmp_path, group)
    for memory_id, body in _BODIES.items():
        legacy.append_record(SharedMemoryRecord(
            memory_id=memory_id, body=body, kind=MemoryKind.PROCEDURE,
            status=SharedMemoryStatus.ACTIVE, injection_policy="always",
            priority=10, agent_instance_id="agent-1",
            created_at=_now_iso(), updated_at=_now_iso(),
        ), assignments=[{"target_type": "agent", "target_id": "agent-1"}])
    intel = RuleMergeStore(tmp_path)
    RuleMergeService(intel).backfill_group(legacy, group)
    # Anchor each Definition back to its legacy memory id via Evidence, so the
    # canonical map can resolve memory_id -> definition_id (the context-level
    # shadow compare relies on evidence source ids).
    for definition in intel.list_definitions():
        intel.upsert_evidence(build_evidence(
            definition_id=definition.definition_id,
            source_rule_id=next(
                memory_id for memory_id, body in _BODIES.items()
                if normalize_rule_text(body) == definition.canonical_text
            ),
            agent_instance_id="agent-1", project_ref="p0", session_id="s0",
            content=definition.canonical_text,
            observed_at=_now_iso(),
        ))
    return legacy, intel


def _activate_canonical(tmp_path, group, intel, legacy, *, activation_status="active"):
    """Persist the Req8 activation row and full readiness.

    Normalizes every active mandatory source link to its resolved active,
    group-bound Definition, builds the projection graph, and records the
    ``rule_canonical_state`` row.  After this, ``canonical_reconciliation_status``
    reports ``canonical_ready == True``.
    """
    bound = {
        binding.definition_id
        for binding in intel.list_bindings(share_group_id=group, status="active")
    }
    for record in legacy.list_records():
        if record.injection_policy != "always":
            continue
        if str(
            getattr(record.status, "value", record.status)
        ) != SharedMemoryStatus.ACTIVE.value:
            continue
        link = intel.get_source_link(group, record.memory_id)
        if not link:
            continue
        target = str(link.get("canonical_definition_id") or "")
        if target in bound:
            continue
        resolved = intel.resolve_canonical(target) if target else ""
        definition = intel.get_definition(resolved) if resolved else None
        if (
            definition is not None
            and str(
                getattr(definition.status, "value", definition.status)
            ) == SharedMemoryStatus.ACTIVE.value
            and resolved in bound
        ):
            intel.upsert_source_link(
                share_group_id=group, memory_id=record.memory_id,
                source_revision=link.get("source_revision", ""),
                original_definition_id=link.get("original_definition_id", ""),
                canonical_definition_id=resolved,
            )
    scope = GovernanceScope(mode="share_group", share_group_id=group)
    out_path = share_group_projection_path(tmp_path, scope)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        _json.dumps(
            build_shared_memory_graph(tmp_path, group), ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    RuleReconciliationStore(intel).set_canonical_activation(
        group, activation_status=activation_status,
        canonical_digest="test-digest", read_path="rule-intelligence",
    )


def _packet(legacy, group, read_path):
    return build_context_packet(
        legacy, task="写测试",
        effective_context=EffectiveAgentContext("agent-1", group),
        read_path=read_path,
    )


def _fields(packet):
    return {
        key: packet.get(key)
        for key in (
            "requested_read_path", "effective_read_path", "fallback_reason",
            "canonical_definitions", "canonical_ready",
        )
    }


def test_legacy_request_never_switches_and_fields_exist(tmp_path):
    legacy, _ = _seed_group(tmp_path)
    packet = _packet(legacy, "g1", MODE_LEGACY)
    fields = _fields(packet)
    # The five Req8 fields are always present.
    assert fields == {
        "requested_read_path": "legacy",
        "effective_read_path": "legacy",
        "fallback_reason": "",
        "canonical_definitions": 0,
        "canonical_ready": False,
    }
    assert packet["read_path"]["mode"] == MODE_LEGACY


def test_rule_intelligence_request_without_activation_falls_back(tmp_path):
    # No rule_canonical_state row at all: activation is missing -> legacy +
    # canonical_not_activated.
    legacy, _ = _seed_group(tmp_path)
    packet = _packet(legacy, "g1", MODE_RULE_INTELLIGENCE)
    assert packet["requested_read_path"] == "rule-intelligence"
    assert packet["effective_read_path"] == "legacy"
    assert packet["fallback_reason"] == "canonical_not_activated"
    assert packet["canonical_ready"] is False
    assert packet["read_path"]["mode"] == MODE_LEGACY


def test_auto_request_without_activation_falls_back(tmp_path):
    legacy, _ = _seed_group(tmp_path)
    packet = _packet(legacy, "g1", "auto")
    assert packet["requested_read_path"] == "auto"
    assert packet["effective_read_path"] == "legacy"
    assert packet["fallback_reason"] == "canonical_not_activated"


def test_inactive_activation_is_not_a_switch(tmp_path):
    # A persisted row with activation_status != "active" is not activation.
    legacy, intel = _seed_group(tmp_path)
    _activate_canonical(
        tmp_path, "g1", intel, legacy, activation_status="inactive",
    )
    packet = _packet(legacy, "g1", MODE_RULE_INTELLIGENCE)
    assert packet["effective_read_path"] == "legacy"
    assert packet["fallback_reason"] == "canonical_not_activated"


def test_activated_but_legacy_request_is_no_fallback(tmp_path):
    # Activation + full readiness exist, but the caller asked for legacy: that
    # is not a fallback, effective stays legacy with no reason.  The Req8 gate
    # is only evaluated for a non-legacy request, so the group-level fields
    # keep their neutral defaults on the legacy path.
    legacy, intel = _seed_group(tmp_path)
    _activate_canonical(tmp_path, "g1", intel, legacy)
    packet = _packet(legacy, "g1", MODE_LEGACY)
    assert packet["requested_read_path"] == "legacy"
    assert packet["effective_read_path"] == "legacy"
    assert packet["fallback_reason"] == ""
    assert packet["canonical_ready"] is False
    assert packet["canonical_definitions"] == 0


def test_activated_but_readiness_incomplete_reports_readiness_failed(tmp_path):
    # Activation row is active but the projection graph is not built, so
    # canonical_reconciliation_status is not ready -> readiness_failed.
    legacy, intel = _seed_group(tmp_path)
    RuleReconciliationStore(intel).set_canonical_activation(
        "g1", activation_status="active",
        canonical_digest="test-digest", read_path="rule-intelligence",
    )
    packet = _packet(legacy, "g1", MODE_RULE_INTELLIGENCE)
    assert packet["effective_read_path"] == "legacy"
    assert packet["fallback_reason"].startswith("readiness_failed:")
    assert "graph_not_built" in packet["fallback_reason"]
    assert packet["canonical_ready"] is False


def test_activated_and_ready_engages_canonical(tmp_path):
    # Both Req8 gates pass: effective_read_path == rule-intelligence.
    legacy, intel = _seed_group(tmp_path)
    _activate_canonical(tmp_path, "g1", intel, legacy)
    packet = _packet(legacy, "g1", MODE_RULE_INTELLIGENCE)
    assert packet["requested_read_path"] == "rule-intelligence"
    assert packet["effective_read_path"] == "rule-intelligence"
    assert packet["fallback_reason"] == ""
    assert packet["canonical_ready"] is True
    assert packet["canonical_definitions"] == 3
    assert packet["read_path"]["mode"] == MODE_RULE_INTELLIGENCE


def test_auto_uses_canonical_when_gate_passes(tmp_path):
    legacy, intel = _seed_group(tmp_path)
    _activate_canonical(tmp_path, "g1", intel, legacy)
    packet = _packet(legacy, "g1", "auto")
    assert packet["requested_read_path"] == "auto"
    assert packet["effective_read_path"] == "rule-intelligence"
    assert packet["canonical_ready"] is True
