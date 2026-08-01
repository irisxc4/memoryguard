"""P3 Rule Intelligence migration tests (P3 §2, §10).

Backfill is lossless: one legacy record becomes one Definition, and every legacy
assignment becomes a Binding (old count == new count).  Dual-write keeps a newly
created rule mirrored into the intelligence layer, and the migration script is
idempotent (schema version marker is written once, re-runs are safe).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.rule_evidence import build_evidence
from memoryguard.rule_merge import RuleMergeService, RuleMergeStore
from memoryguard.rule_merge_store import iter_legacy_groups
from memoryguard.schema_v3 import (
    EffectiveAgentContext,
    MemoryKind,
    RuleMatchReceipt,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
)
from memoryguard.shared_memory_store import SharedMemoryStore


def _seed_group(tmp_path, group_id: str, *, records: list[tuple[str, str]]):
    """Seed a legacy group with mandatory rules + assignments + receipts."""
    store = SharedMemoryStore(tmp_path, group_id)
    for i, (memory_id, body) in enumerate(records):
        store.append_record(SharedMemoryRecord(
            memory_id=memory_id, body=body, kind=MemoryKind.PROCEDURE,
            status=SharedMemoryStatus.ACTIVE, injection_policy="always",
            priority=10, agent_instance_id=f"agent-{group_id}-{i}",
            created_at=_now_iso(), updated_at=_now_iso(),
        ), assignments=[{"target_type": "agent", "target_id": f"agent-{group_id}-{i}"}])
        store.append_rule_match_receipt(RuleMatchReceipt(
            receipt_id=f"receipt-{group_id}-{memory_id}",
            memory_id=memory_id, share_group_id=group_id,
            agent_instance_id=f"agent-{group_id}-{i}",
            task_hash="t", task="task",
            project_ref=str(tmp_path / "project"),
            created_at=_now_iso(),
        ))
    return store


def test_backfill_is_lossless_record_to_definition_assignment_to_binding(tmp_path):
    group = "g1"
    _seed_group(tmp_path, group, records=[("r1", "提交代码前必须运行测试"), ("r2", "使用 pnpm 安装依赖")])
    legacy = SharedMemoryStore(tmp_path, group)
    assignments_before = len(legacy.list_rule_assignments())
    records_before = len(legacy.list_records())

    service = RuleMergeService(RuleMergeStore(tmp_path))
    ledger = service.backfill_legacy(tmp_path, only_group=group)
    group_ledger = ledger["per_group"][group]
    assert group_ledger["records"] == records_before == 2
    assert group_ledger["definitions"] == records_before
    assert group_ledger["assignments"] == assignments_before
    assert group_ledger["bindings"] == assignments_before
    assert group_ledger["receipts"] == records_before
    assert group_ledger["evidence"] == records_before
    assert ledger["migration_loss"] == 0


def test_backfill_covers_multiple_groups(tmp_path):
    _seed_group(tmp_path, "team-a", records=[("ra", "提交代码前必须运行测试")])
    _seed_group(tmp_path, "team-b", records=[("rb", "使用 pnpm 安装依赖")])
    service = RuleMergeService(RuleMergeStore(tmp_path))
    ledger = service.backfill_legacy(tmp_path)
    assert ledger["groups"] == 2
    assert set(ledger["per_group"]) == {"team-a", "team-b"}
    assert ledger["totals"]["records"] == 2
    assert ledger["totals"]["bindings"] == 2
    # different groups -> distinct bindings keep their own share_group_id
    bindings = RuleMergeStore(tmp_path).list_bindings()
    assert {b.share_group_id for b in bindings} == {"team-a", "team-b"}


def test_backfill_synonym_rules_become_candidates_not_forced_merge(tmp_path):
    group = "g1"
    _seed_group(tmp_path, group, records=[
        ("r1", "提交代码前必须运行测试"),
        ("r2", "提交前必须执行测试"),
    ])
    service = RuleMergeService(RuleMergeStore(tmp_path))
    service.backfill_legacy(tmp_path, only_group=group)
    store = RuleMergeStore(tmp_path)
    # Two distinct canonical spellings -> two definitions, but the semantic
    # hash matches, so the scan surfaces a merge candidate.  Seed enough
    # independent evidence across agents/projects to satisfy the auto criteria.
    assert store.count_definitions() == 2
    for definition in store.list_definitions():
        for i in range(3):
            store.upsert_evidence(build_evidence(
                definition_id=definition.definition_id,
                source_rule_id=f"{definition.definition_id}-ev{i}",
                agent_instance_id=f"agent-{i}",
                project_ref=f"project-{i}",
                session_id=f"session-{i}",
                content=definition.canonical_text,
            ))
    proposals = service.scan_and_propose()
    assert proposals
    assert any(p["status"] == "candidate" for p in proposals)


def test_iter_legacy_groups_skips_missing_db(tmp_path):
    _seed_group(tmp_path, "g1", records=[("r1", "提交代码前必须运行测试")])
    groups = list(iter_legacy_groups(tmp_path))
    assert len(groups) == 1
    assert groups[0][0] == "g1"


def test_migration_script_is_idempotent(tmp_path):
    from memoryguard.migrations.rule_definition_v1 import migrate

    db_path = tmp_path / "migration.db"
    first = migrate(str(db_path))
    second = migrate(str(db_path))
    assert first["schema_version"] == second["schema_version"]
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    for table in ("rule_definitions", "rule_bindings", "rule_evidence",
                  "rule_merge_proposals", "rule_merge_decisions"):
        assert table in tables
    conn = sqlite3.connect(db_path)
    try:
        value = conn.execute(
            "SELECT value FROM schema_meta WHERE key='rule-intelligence-v1'"
        ).fetchone()
    finally:
        conn.close()
    assert value is not None


def test_dual_write_syncs_new_rule(tmp_path):
    from memoryguard.rule_creation import RuleCreationService

    group = "g1"
    AgentBindingStore(tmp_path).bind_agent("agent-a", group)
    store = SharedMemoryStore(tmp_path, group)
    intelligence = RuleMergeStore(tmp_path)
    context = EffectiveAgentContext(
        agent_instance_id="agent-a", share_group_id=group,
        project_ref=str(tmp_path / "project"), session_id="s1",
    )
    service = RuleCreationService(
        tmp_path, group, store=store,
        merge_service=RuleMergeService(intelligence),
    )
    decision = service.create_rule_from_text("提交代码前必须运行测试", context)
    assert decision.status == "created"
    definitions = intelligence.list_definitions()
    assert len(definitions) == 1
    bindings = intelligence.list_bindings()
    assert len(bindings) >= 1
