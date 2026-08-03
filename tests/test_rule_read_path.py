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

from types import SimpleNamespace

import pytest

from memoryguard.access_context import AccessContext
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


@pytest.fixture
def canonical_readiness_ready(monkeypatch):
    """Provide explicit public readiness evidence for legacy canonical tests."""
    original_open = RuleReadPath._open

    def _open(read):
        store = original_open(read)
        if store is None or getattr(store, "_test_readiness_gate", False):
            return store
        base_metrics = store.metrics

        def metrics():
            result = dict(base_metrics())
            result["binding_contribution_diff"] = 0
            return result

        store.metrics = metrics
        store.shadow_summary = lambda: {
            "missing": [], "extra": [], "permission_diff": 0,
        }
        store._test_readiness_gate = True
        return store

    monkeypatch.setattr(RuleReadPath, "_open", _open)


# ---------------------------------------------------------------------------
# mode resolution
# ---------------------------------------------------------------------------


def test_read_path_mode_normalizes_and_falls_back():
    assert resolve_read_path_mode("auto") == MODE_AUTO
    assert resolve_read_path_mode("legacy") == MODE_LEGACY
    assert resolve_read_path_mode("rule-intelligence") == MODE_RULE_INTELLIGENCE
    assert resolve_read_path_mode("bogus") in {MODE_AUTO, MODE_LEGACY}


def test_read_path_mode_defaults_to_legacy(monkeypatch):
    monkeypatch.delenv("MEMORYGUARD_RULE_READ_PATH", raising=False)
    assert resolve_read_path_mode() == MODE_LEGACY


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
        context = AccessContext("test-admin", True, True, False)
        token = intel.issue_merge_capability(pid, context)
        intel.approve_proposal(
            pid, approved_by=context.principal,
            capability_token=token, access_context=context,
        )
        result = service.merge_proposal(pid, actor="admin")
        assert result["ok"] is True
    return legacy, intel


def test_canonical_map_maps_evidence_source_ids(
    tmp_path, canonical_readiness_ready,
):
    legacy, intel = _seed_intelligence_pair(tmp_path, merge=False)
    read = RuleReadPath(tmp_path, "g1")
    mapping = read.resolve_canonical_map(known_memory_ids={"m1", "m2", "m3"})
    assert mapping is not None
    assert mapping["mode"] == MODE_RULE_INTELLIGENCE
    # Every mapped memory id exists in the legacy store.
    assert set(mapping["memory_to_definition"]) <= {"m1", "m2", "m3"}


def test_canonical_map_drops_stale_evidence(
    tmp_path, canonical_readiness_ready,
):
    _seed_intelligence_pair(tmp_path, merge=False)
    read = RuleReadPath(tmp_path, "g1")
    # known_memory_ids excludes m3 -> no canonical mapping may reference it.
    mapping = read.resolve_canonical_map(known_memory_ids={"m1", "m2"})
    assert mapping is not None
    for memory_id in mapping["memory_to_definition"]:
        assert memory_id in {"m1", "m2"}


def test_dedupe_records_collapses_merged_duplicates(
    tmp_path, canonical_readiness_ready,
):
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


def test_bootstrap_injects_merged_rule_once(
    tmp_path, canonical_readiness_ready,
):
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


class _ReadinessStore:
    """Small public-API double; no private Store state is consumed by gate."""

    def __init__(
        self,
        *,
        projection_lag=0,
        projection_error="",
        migration_loss=0,
        binding_contribution_diff=0,
        include_binding_diff=True,
        shadow=None,
        metric_extras=None,
    ):
        self._projection = {
            "projection_lag": projection_lag,
            "projection_error": projection_error,
        }
        self._metrics = {
            "migration_loss": migration_loss,
            **(metric_extras or {}),
        }
        if include_binding_diff:
            self._metrics["binding_contribution_diff"] = binding_contribution_diff
        self._shadow = shadow or {
            "missing": [], "extra": [], "permission_diff": 0,
        }

    def projection_status(self):
        return dict(self._projection)

    def metrics(self):
        return dict(self._metrics)

    def shadow_summary(self):
        return dict(self._shadow)

    def list_definitions(self, status="active"):
        return [SimpleNamespace(
            definition_id="definition-1",
            rule_strength=0.0,
            maturity_state="mature",
        )]

    def list_bindings(self, share_group_id=None, status="active"):
        return [SimpleNamespace(definition_id="definition-1")]

    def list_evidence(self, definition_id=None):
        # Kept for compatibility; Evidence no longer establishes Source
        # ownership — Source Links below are the canonical-map fact table.
        return [SimpleNamespace(source_rule_id="m1")]

    def list_source_links(
        self,
        *,
        share_group_id=None,
        status=None,
        canonical_definition_id=None,
    ):
        links = [{
            "share_group_id": "g1",
            "memory_id": "m1",
            "original_definition_id": "definition-1",
            "canonical_definition_id": "definition-1",
            "status": "active",
        }]
        if share_group_id is not None:
            links = [l for l in links if l["share_group_id"] == share_group_id]
        if status is not None:
            links = [l for l in links if l["status"] == status]
        if canonical_definition_id is not None:
            links = [
                l for l in links
                if l["canonical_definition_id"] == canonical_definition_id
            ]
        return links

    def get_definition(self, definition_id):
        if definition_id != "definition-1":
            return None
        return SimpleNamespace(
            definition_id="definition-1",
            status="active",
            rule_strength="must",
            maturity_state="validated",
        )

    def resolve_canonical(self, definition_id):
        return definition_id


def _readiness_reader(store):
    read = RuleReadPath(".", "g1")
    read._store = store
    return read


@pytest.mark.parametrize(
    "store_kwargs, shadow_summary, failure",
    [
        ({"projection_lag": 1}, None, "projection_lag_nonzero"),
        ({"projection_error": "stale projection"}, None,
         "projection_error_present"),
        ({"migration_loss": 1}, None, "migration_loss_nonzero"),
        ({"binding_contribution_diff": 1}, None,
         "binding_contribution_diff_nonzero"),
        ({}, {"missing": [], "extra": [], "permission_diff": 1},
         "shadow_permission_diff_nonzero"),
    ],
    ids=["projection-lag", "projection-error", "migration-loss",
         "binding-diff", "shadow-permission-diff"],
)
def test_canonical_readiness_failure_falls_back(
    store_kwargs, shadow_summary, failure,
):
    read = _readiness_reader(_ReadinessStore(**store_kwargs))
    mapping = read.resolve_canonical_map(
        known_memory_ids={"m1"}, shadow_summary=shadow_summary,
    )
    assert mapping is None
    assert read.last_readiness["ready"] is False
    assert failure in read.last_readiness["failures"]


def test_canonical_readiness_missing_binding_diff_fails_closed_with_wiring():
    read = _readiness_reader(
        _ReadinessStore(include_binding_diff=False),
    )
    assert read.resolve_canonical_map(known_memory_ids={"m1"}) is None
    readiness = read.last_readiness
    assert "binding_contribution_diff_unavailable" in readiness["failures"]
    assert any(
        "Store.metrics() must expose binding_contribution_diff" in item
        for item in readiness["wiring_requirements"]
    )


def test_canonical_readiness_ready_allows_map_and_ignores_broad_type_counters():
    read = _readiness_reader(_ReadinessStore(
        metric_extras={"system_auto_binding": 4, "auto_broad_binding": 9},
    ))
    mapping = read.resolve_canonical_map(known_memory_ids={"m1"})
    assert mapping is not None
    assert mapping["memory_to_definition"] == {"m1": "definition-1"}
    assert read.last_readiness["ready"] is True


def test_canonical_readiness_requires_all_shadow_audience_diffs_zero():
    for field in ("missing", "extra"):
        read = _readiness_reader(_ReadinessStore())
        diff = {"missing": [], "extra": [], "permission_diff": 0}
        diff[field] = ["m1"]
        assert read.resolve_canonical_map(
            known_memory_ids={"m1"}, shadow_summary=diff,
        ) is None
        assert f"shadow_{field}_nonzero" in read.last_readiness["failures"]


class _DanglingAliasStore(_ReadinessStore):
    """Source link resolves to an alias whose resolver returns a dangling
    non-active target — it must never enter the canonical map."""

    def list_source_links(self, *, share_group_id=None, status=None,
                          canonical_definition_id=None):
        return [{
            "share_group_id": "g1",
            "memory_id": "m1",
            "original_definition_id": "definition-alias",
            "canonical_definition_id": "definition-alias",
            "status": "active",
        }]

    def get_definition(self, definition_id):
        if definition_id != "definition-alias":
            return None
        return SimpleNamespace(
            definition_id="definition-alias",
            status="alias",
            rule_strength="must",
            maturity_state="validated",
        )

    def resolve_canonical(self, definition_id):
        # The resolver returns the alias itself: a dangling alias with no
        # superseded_by.  Strict active-only enforcement must fail closed.
        return definition_id


def test_dangling_alias_never_enters_canonical_map():
    read = _readiness_reader(_DanglingAliasStore())
    mapping = read.resolve_canonical_map(known_memory_ids={"m1"})
    # Readiness only gates wiring (APIs present); the dangling-alias rejection
    # is a data-level fail-closed inside resolve_canonical_map, so the map is
    # None even though the store is wired.
    assert mapping is None


def test_canonical_read_cross_agent_keeps_each_agents_rule(
    tmp_path, canonical_readiness_ready,
):
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


def test_canonical_read_cross_project_keeps_each_projects_rule(
    tmp_path, canonical_readiness_ready,
):
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


def test_canonical_read_applies_exclude_before_dedupe(
    tmp_path, canonical_readiness_ready,
):
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


def test_shadowed_record_never_replaces_active_representative(
    tmp_path, canonical_readiness_ready,
):
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
