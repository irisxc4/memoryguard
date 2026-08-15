"""Acceptance coverage for unified V2 semantic governance and budget recovery."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from memoryguard.auto_organizer import AutoOrganizer
from memoryguard.context_bootstrap import build_context_packet
from memoryguard.memory import MemoryReadScope
from memoryguard.rule_merge_store import RuleMergeStore
from memoryguard.rule_reconciliation import build_bundles
from memoryguard.schema_v3 import MemoryKind, SharedMemoryStatus
from memoryguard.runtime_v2.context_budget import ContextBudget
from memoryguard.runtime_v2.context_engine import ContextEngine


NEAR_DEFAULT_RULES = (
    "默认使用 caveman/RTK",
    "默认使用 caveman 和 RTK",
    "默认用 caveman/RTK",
    "默认使用 caveman + RTK",
    "默认使用 caveman、RTK",
    "默认使用 caveman/RTK，作为默认工具",
)

# Shape copied from observed V2 state only.  Test uses an in-memory fixture
# object; it never opens or mutates the user's MemoryGuard database.
OBSERVED_ACTIVE_RULE_FIXTURE = (
    ("0a6539c12f03dcf1", "全局默认使用 caveman 和 RTK，主 Agent 与所有子代理"),
    ("f2746e8e191feb54", "用户要求全平台默认使用 caveman 和 RTK"),
    ("dc3feca06b7861ab", "全局默认使用 caveman 和 RTK，Luna 并行规则"),
)


def _write(
    organizer: AutoOrganizer,
    *,
    body: str,
    event_id: str,
    agent: str = "agent-a",
    policy: str = "always",
) -> dict:
    return organizer.write({
        "event_id": event_id,
        "agent_instance_id": agent,
        "share_group_id": "group-a",
        "body": body,
        "kind": "procedure",
        "injection_policy": policy,
        "priority": 10,
    })


def _atoms(organizer: AutoOrganizer):
    return organizer.store.list_atoms(
        scope=MemoryReadScope(
            workspace_id=str(organizer.service.workspace),
            share_group_id="group-a",
            admin=True,
        ),
        include_building=True,
    )


def test_six_near_default_tool_rules_leave_one_effective_canonical_atom(tmp_path: Path) -> None:
    organizer = AutoOrganizer(tmp_path, "group-a")
    receipts: list[str] = []
    for index, body in enumerate(NEAR_DEFAULT_RULES):
        result = _write(organizer, body=body, event_id=f"near-{index}")
        receipt = result.get("governance_receipt") or {}
        if receipt:
            receipts.append(str(receipt.get("action") or ""))

    atoms = _atoms(organizer)
    active = [atom for atom in atoms if atom.status == "active"]
    assert len(active) == 1
    assert "caveman" in active[0].body.casefold()
    assert "rtk" in active[0].body.casefold()
    assert set(receipts) <= {"merged", "updated", "superseded", "unchanged"}
    assert "merged" in receipts
    assert "superseded" in receipts


def test_extended_default_rule_supersedes_old_rule_in_same_scope(tmp_path: Path) -> None:
    organizer = AutoOrganizer(tmp_path, "group-a")
    first = _write(organizer, body="默认使用 X", event_id="base")
    second = _write(
        organizer,
        body="默认使用 X，子代理也使用",
        event_id="extended",
    )

    assert first["mutation_kind"] == "created"
    assert second["mutation_kind"] == "superseded"
    assert second["governance_receipt"]["action"] == "superseded"
    assert second["governance_receipt"]["old_id"] == first["memory_id"]
    atoms = _atoms(organizer)
    assert len([atom for atom in atoms if atom.status == "active"]) == 1
    assert len([atom for atom in atoms if atom.status == "superseded"]) == 1
    assert "子代理" in second["atom"].body


def test_same_rule_different_agent_scope_is_not_merged(tmp_path: Path) -> None:
    organizer = AutoOrganizer(tmp_path, "group-a")
    first = _write(
        organizer,
        body="默认使用 caveman/RTK",
        event_id="agent-a-rule",
        agent="agent-a",
    )
    second = _write(
        organizer,
        body="默认使用 caveman/RTK",
        event_id="agent-b-rule",
        agent="agent-b",
    )

    assert first["mutation_kind"] == "created"
    assert second["mutation_kind"] == "created"
    assert first["memory_id"] != second["memory_id"]
    active = [atom for atom in _atoms(organizer) if atom.status == "active"]
    assert len(active) == 2
    assert {atom.agent_instance_id for atom in active} == {"agent-a", "agent-b"}


def test_opposite_rules_enter_conflict_governance_instead_of_concatenation(tmp_path: Path) -> None:
    organizer = AutoOrganizer(tmp_path, "group-a")
    _write(organizer, body="默认使用 caveman/RTK", event_id="positive")
    conflict = _write(
        organizer,
        body="不要默认使用 caveman/RTK",
        event_id="negative",
    )

    assert conflict["mutation_kind"] == "conflicted"
    assert conflict["governance_receipt"]["action"] == "conflicted"
    atoms = _atoms(organizer)
    conflicted = [atom for atom in atoms if atom.status == "conflicted"]
    assert len(conflicted) == 2
    group_ids = {str(atom.metadata.get("conflict_group_id") or "") for atom in conflicted}
    assert len(group_ids) == 1
    assert "" not in group_ids


def test_mandatory_semantic_duplicates_are_collapsed_before_budget_lock() -> None:
    engine = ContextEngine(
        ready=True,
        state="V2_ACTIVE",
        budget=ContextBudget(
            mandatory_max_items=1,
            mandatory_max_chars=6000,
            mandatory_max_tokens=6000,
        ),
    )
    packet = engine.bootstrap(
        {
            "task": "use default tools",
            "trusted_identity": {"agent": "agent-a", "group": "group-a"},
        },
        {
            "mandatory": [
                {
                    "item_id": f"rule-{index}",
                    "body": body,
                    "kind": "procedure",
                    "is_rule": True,
                    "injection_policy": "always",
                    "scope": {
                        "agent_instance_id": "agent-a",
                        "share_group_id": "group-a",
                    },
                }
                for index, body in enumerate(NEAR_DEFAULT_RULES)
            ]
        },
    ).to_dict()

    assert packet["status"] == "ok"
    assert packet["error"] == ""
    assert len(packet["mandatory"]) == 1
    assert packet["budget"]["mandatory"]["items"] == 1
    omitted_reasons = {
        receipt["reason"]
        for receipt in packet["receipts"]
        if not receipt["hit"]
    }
    assert omitted_reasons & {"governance_duplicate", "governance_update_shadowed"}


class _LegacyRules:
    def __init__(self, assignments: dict[str, list[SimpleNamespace]]) -> None:
        self.assignments = assignments

    def list_rule_assignments(self, memory_id: str):
        return list(self.assignments.get(memory_id, ()))


def _assignment(agent: str) -> SimpleNamespace:
    return SimpleNamespace(
        target_type="agent",
        target_id=agent,
        project_ref="",
        provider="",
        runtime_role="",
        effect="include",
        priority_override=None,
    )


def test_rule_reconciliation_uses_same_semantics_and_keeps_agent_scopes_separate(tmp_path: Path) -> None:
    store = RuleMergeStore(tmp_path)
    records = [
        SimpleNamespace(
            memory_id=f"near-{index}",
            body=body,
            priority=10,
            updated_at=f"2026-08-14T00:00:{index:02d}+00:00",
        )
        for index, body in enumerate(NEAR_DEFAULT_RULES)
    ]
    legacy = _LegacyRules({record.memory_id: [_assignment("agent-a")] for record in records})
    plan = build_bundles(store, legacy, "group-a", records, workspace=tmp_path)

    assert len(plan["bundles"]) == 1
    bundle = plan["bundles"][0]
    assert set(bundle.source_memory_ids) == {record.memory_id for record in records}
    assert "caveman" in bundle.body.casefold()
    assert "rtk" in bundle.body.casefold()

    cross_scope_records = [
        SimpleNamespace(memory_id="agent-a", body="默认使用 caveman/RTK", priority=10, updated_at="1"),
        SimpleNamespace(memory_id="agent-b", body="默认使用 caveman/RTK", priority=10, updated_at="2"),
    ]
    cross_scope = _LegacyRules({
        "agent-a": [_assignment("agent-a")],
        "agent-b": [_assignment("agent-b")],
    })
    cross_plan = build_bundles(
        store,
        cross_scope,
        "group-a",
        cross_scope_records,
        workspace=tmp_path,
    )
    assert len(cross_plan["bundles"]) == 2


def test_observed_active_rule_shape_collapses_before_mandatory_budget(tmp_path: Path) -> None:
    class FixtureStore:
        workspace = tmp_path
        group_id = "group-a"

        def __init__(self) -> None:
            self.records = [
                SimpleNamespace(
                    memory_id=memory_id,
                    body=body,
                    kind=MemoryKind.PROCEDURE,
                    status=SharedMemoryStatus.ACTIVE,
                    injection_policy="always",
                    priority=10,
                    locked=False,
                    confidence=0.9,
                    created_at="2026-08-14T00:00:00+00:00",
                    updated_at="2026-08-14T00:00:00+00:00",
                    dedup_domain="always:21a3eaa94979afee",
                )
                for memory_id, body in OBSERVED_ACTIVE_RULE_FIXTURE
            ]
            self.records.extend(
                SimpleNamespace(
                    memory_id=f"relevant-{index}",
                    body=f"unrelated active memory {index}",
                    kind=MemoryKind.FACT,
                    status=SharedMemoryStatus.ACTIVE,
                    injection_policy="relevant",
                    priority=0,
                    locked=False,
                    confidence=0.5,
                    created_at="2026-08-14T00:00:00+00:00",
                    updated_at="2026-08-14T00:00:00+00:00",
                    dedup_domain="relevant",
                )
                for index in range(5)
            )

        def list_records(self):
            return list(self.records)

        def list_rule_assignments(self):
            return []

        def get_active_version_id(self):
            return "fixture-version"

    packet = build_context_packet(
        FixtureStore(),
        task="use default tools",
        max_items=12,
        max_chars=6000,
        read_path="legacy",
    )

    assert packet["error"] == ""
    assert packet["status"] == "ok"
    assert len(packet["context_packet"]["mandatory_items"]) == 1
    assert packet["budget"]["mandatory_used_items"] == 1
    assert {item["action"] for item in packet["audit_actions"]} == {"collapse"}
    assert {item["old_id"] for item in packet["audit_actions"]} == {
        "f2746e8e191feb54",
        "0a6539c12f03dcf1",
    }
