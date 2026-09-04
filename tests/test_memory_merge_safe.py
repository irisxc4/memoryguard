"""Narrow regressions for memory-plane safe atom supersede MCP."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.cutover_v2.surfaces import MCP_MUTATION_NAMES, MCP_TOOL_NAMES
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.mcp_server import TOOLS, _MUTATING_TOOLS, _V2_MEMORY_MERGE_TOOLS, _validate_v2_mcp_arguments
from memoryguard.memory import MemoryAtom, MemoryAtomStore
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


GROUP = "group-a"
OTHER_GROUP = "group-b"
BODY = "Always use rtk for shell commands before every command."
CONFLICT_BODY = "Never use rtk for shell commands"
WORKMODE_CANONICAL = (
    "长期协作规则：不得为了迎合用户意见而附和。面对用户的判断、方案、指令或结论，"
    "先基于可得证据、风险、约束与真实目标进行客观分析。"
    "默认对所有编程、代码修改、重构、修复、评审、技术设计与依赖选择任务启用 Ponytail（full），"
    "并与 Caveman、RTK 一起作为主 Agent 和子 Agent 的默认工作方式。"
    "用户说“stop ponytail”或“normal mode”时当前会话停用。"
)
WORKMODE_DUPLICATE = "全局默认使用 caveman 和 RTK，主 Agent 与所有子代理也默认遵循，除非用户明确要求关闭。"


class _Manifest:
    def __init__(self, state: str = "V2_ACTIVE", generation: int = 7):
        self.state = state
        self.generation = generation

    def current(self):
        return {"state": self.state, "generation": self.generation}


def _context(workspace: Path, *, admin: bool = True, group: str = GROUP, agent: str = "admin"):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id=f"session-{agent}",
            session_source="host",
            session_trusted=True,
        ),
        workspace_id=str(workspace),
        share_group_id=group,
        project_ref="project-a",
        provider="codex",
    )


def _port(workspace: Path) -> NativeV2RuntimePort:
    return NativeV2RuntimePort(workspace, state_provider=_Manifest())


def _seed_atom(
    root: Path,
    memory_id: str,
    *,
    body: str = BODY,
    group: str = GROUP,
    agent: str = "agent-a",
    policy: str = "always",
    priority: int = 10,
    revision: int | None = None,
) -> MemoryAtom:
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    context = V2MutationContext(
        workspace_id=str(root.resolve()),
        share_group_id=group,
        agent_instance_id=agent,
        project_ref="project-a",
        provider="codex",
        runtime_role="test",
        actor=agent,
        admin=True,
        authority="admin",
    )
    atom, _decision = governance.put_atom(
        MemoryAtom(
            memory_id=memory_id,
            body=body,
            kind="procedure",
            injection_policy=policy,
            priority=priority,
            revision=revision or 1,
            share_group_id=group,
            agent_instance_id=agent,
            project_ref="project-a",
            provider="codex",
            runtime_role="test",
            workspace_id=str(root.resolve()),
            metadata={
                "owner_agent_id": agent,
                "audience": {
                    "source": "native_v2",
                    "target_type": "group",
                    "target_id": group,
                    "effect": "include",
                },
            },
        ),
        context=context,
        evidence=[{"source_ref": f"seed/{memory_id}"}],
        reason=f"seed {memory_id}",
        idempotency_key=f"seed-{group}-{memory_id}",
    )
    memory.project_evidence(evidence)
    memory.set_visibility("active")
    return memory.get_atom(
        memory_id,
        scope={"workspace_id": str(root.resolve()), "share_group_id": group, "admin": True},
    ) or atom


def _snapshot(root: Path) -> tuple[list[tuple], list[tuple], list[tuple]]:
    memory = MemoryAtomStore(root)
    with sqlite3.connect(memory.db_path) as conn:
        atoms = conn.execute(
            "SELECT atom_id,memory_id,status,revision,injection_policy,priority FROM atoms ORDER BY memory_id,atom_id",
        ).fetchall()
        edges = conn.execute(
            "SELECT old_atom_id,new_atom_id FROM supersession_edges ORDER BY old_atom_id,new_atom_id",
        ).fetchall()
    decisions = [
        (item.decision_id, item.operation, item.status)
        for item in GovernanceV2(root, memory_store=memory).list_decisions()
        if item.operation == "supersede"
    ]
    return atoms, edges, decisions


def _dispatch(root: Path, name: str, payload: dict, *, admin: bool = True, group: str = GROUP, agent: str = "admin"):
    return _port(root).dispatch_mcp(
        name,
        payload,
        context=_context(root, admin=admin, group=group, agent=agent),
        generation=7,
        state="V2_ACTIVE",
    )


def _pair_payload(canonical: MemoryAtom, duplicate: MemoryAtom, **extra) -> dict:
    payload = {
        "canonical_memory_id": canonical.memory_id,
        "duplicate_memory_id": duplicate.memory_id,
    }
    payload.update(extra)
    return payload


def test_merge_safe_supersede_drops_duplicate_from_mandatory_bootstrap(tmp_path: Path) -> None:
    canonical = _seed_atom(tmp_path, "memory-canonical", priority=80, revision=4)
    duplicate = _seed_atom(tmp_path, "3bf-duplicate", agent="admin", priority=10, revision=2)
    preview = _dispatch(tmp_path, "memoryguard_memory_merge_safe_preview", _pair_payload(canonical, duplicate))
    assert preview["ok"] is True, preview
    data = preview["data"]
    assert data["canonical"]["memory_id"] == canonical.memory_id
    assert data["duplicate"]["memory_id"] == duplicate.memory_id
    assert data["canonical"]["injection_policy"] == "always"
    assert data["relation"] in {"exact", "equivalent", "update"}
    executed = _dispatch(
        tmp_path,
        "memoryguard_memory_merge_safe",
        _pair_payload(
            canonical,
            duplicate,
            confirmed=True,
            expected_atom_revisions=data["expected_atom_revisions"],
            mutation_receipt={"receipt_id": "receipt-memory-merge"},
            idempotency_key="memory-merge-1",
        ),
    )
    assert executed["ok"] is True, executed
    result = executed["data"]
    assert result["canonical"]["status"] == "active"
    assert result["duplicate"]["status"] == "superseded"
    assert result["decision_id"]
    memory = MemoryAtomStore(tmp_path)
    scope = {"workspace_id": str(tmp_path.resolve()), "share_group_id": GROUP, "admin": True}
    assert memory.get_atom(canonical.memory_id, scope=scope).status == "active"
    assert memory.get_atom(duplicate.memory_id, scope=scope).status == "superseded"
    def _mandatory_ids(result: dict) -> set[str]:
        assert result["ok"] is True, result
        rows = result["data"].get("mandatory") or []
        return {str(item.get("memory_id") or item.get("item_id") or "") for item in rows}

    admin_ids = _mandatory_ids(
        _dispatch(tmp_path, "memoryguard_context_bootstrap", {"task": "Always use rtk for shell commands"}),
    )
    assert duplicate.memory_id not in admin_ids
    owner_ids = _mandatory_ids(
        _dispatch(
            tmp_path,
            "memoryguard_context_bootstrap",
            {"task": "Always use rtk for shell commands"},
            agent="agent-a",
        ),
    )
    assert canonical.memory_id in owner_ids
    assert duplicate.memory_id not in owner_ids


def test_preview_is_zero_write_and_feeds_execute(tmp_path: Path) -> None:
    canonical = _seed_atom(tmp_path, "memory-canonical", priority=80)
    duplicate = _seed_atom(tmp_path, "3bf-duplicate", agent="admin", priority=10)
    before = _snapshot(tmp_path)
    preview = _dispatch(
        tmp_path,
        "memoryguard_memory_merge_safe_preview",
        {
            "canonical_atom_id": canonical.atom_id,
            "duplicate_atom_id": duplicate.atom_id,
        },
    )
    assert preview["ok"] is True, preview
    assert _snapshot(tmp_path) == before
    executed = _dispatch(
        tmp_path,
        "memoryguard_memory_merge_safe",
        {
            "canonical_atom_id": canonical.atom_id,
            "duplicate_atom_id": duplicate.atom_id,
            "confirmed": True,
            "expected_atom_revisions": preview["data"]["expected_atom_revisions"],
            "mutation_receipt": {"receipt_id": "receipt-from-preview"},
            "idempotency_key": "memory-merge-preview-feed",
        },
    )
    assert executed["ok"] is True, executed
    assert executed["data"]["duplicate"]["status"] == "superseded"


def test_merge_safe_preview_accepts_claim_subset_and_rejects_opposite_without_writes(tmp_path: Path) -> None:
    canonical = _seed_atom(
        tmp_path,
        "memory-workmode-canonical",
        body=WORKMODE_CANONICAL,
        priority=80,
        revision=4,
    )
    duplicate = _seed_atom(
        tmp_path,
        "memory-workmode-duplicate",
        body=WORKMODE_DUPLICATE,
        agent="admin",
        priority=10,
        revision=2,
    )
    before = _snapshot(tmp_path)

    preview = _dispatch(
        tmp_path,
        "memoryguard_memory_merge_safe_preview",
        _pair_payload(canonical, duplicate),
    )

    assert preview["ok"] is True, preview
    assert preview["data"]["relation"] == "update"
    assert _snapshot(tmp_path) == before

    prohibited = _seed_atom(
        tmp_path,
        "memory-workmode-prohibited",
        body="禁止默认使用 Caveman 和 RTK。",
        agent="admin",
        priority=10,
    )
    before_reject = _snapshot(tmp_path)
    rejected = _dispatch(
        tmp_path,
        "memoryguard_memory_merge_safe_preview",
        _pair_payload(canonical, prohibited),
    )

    assert rejected["ok"] is False, rejected
    assert rejected["code"] == "memory_merge_pair_not_mergeable"
    assert _snapshot(tmp_path) == before_reject


def test_merge_safe_fail_closed_zero_writes(tmp_path: Path) -> None:
    canonical = _seed_atom(tmp_path, "memory-canonical", priority=80)
    duplicate = _seed_atom(tmp_path, "3bf-duplicate", agent="admin", priority=10)
    conflict = _seed_atom(tmp_path, "conflict", body=CONFLICT_BODY, priority=10)
    distinct = _seed_atom(tmp_path, "distinct", body="Never share secrets in prompt text", priority=10)
    weaker = _seed_atom(tmp_path, "weaker", policy="relevant", priority=0)
    stronger_dup = _seed_atom(tmp_path, "stronger-dup", agent="admin", priority=90)
    foreign = _seed_atom(tmp_path, "foreign", group=OTHER_GROUP, priority=10)
    revisions = {
        canonical.atom_id: int(canonical.revision or 0),
        duplicate.atom_id: int(duplicate.revision or 0),
    }
    before = _snapshot(tmp_path)
    cases = [
        (
            "native_admin_capability_required",
            _pair_payload(canonical, duplicate, confirmed=True, expected_atom_revisions=revisions, mutation_receipt={"receipt_id": "r-admin"}, idempotency_key="k-admin"),
            {"admin": False},
        ),
        (
            "target_not_found",
            _pair_payload(canonical, foreign, confirmed=True, expected_atom_revisions={canonical.atom_id: int(canonical.revision or 0), foreign.atom_id: int(foreign.revision or 0)}, mutation_receipt={"receipt_id": "r-cross"}, idempotency_key="k-cross"),
            {},
        ),
        (
            "memory_merge_pair_not_mergeable",
            _pair_payload(canonical, conflict, confirmed=True, expected_atom_revisions={canonical.atom_id: int(canonical.revision or 0), conflict.atom_id: int(conflict.revision or 0)}, mutation_receipt={"receipt_id": "r-conflict"}, idempotency_key="k-conflict"),
            {},
        ),
        (
            "memory_merge_pair_not_mergeable",
            _pair_payload(canonical, distinct, confirmed=True, expected_atom_revisions={canonical.atom_id: int(canonical.revision or 0), distinct.atom_id: int(distinct.revision or 0)}, mutation_receipt={"receipt_id": "r-distinct"}, idempotency_key="k-distinct"),
            {},
        ),
        (
            "memory_merge_pair_not_mergeable",
            _pair_payload(weaker, duplicate, confirmed=True, expected_atom_revisions={weaker.atom_id: int(weaker.revision or 0), duplicate.atom_id: int(duplicate.revision or 0)}, mutation_receipt={"receipt_id": "r-reverse"}, idempotency_key="k-reverse"),
            {},
        ),
        (
            "memory_merge_pair_not_mergeable",
            _pair_payload(canonical, stronger_dup, confirmed=True, expected_atom_revisions={canonical.atom_id: int(canonical.revision or 0), stronger_dup.atom_id: int(stronger_dup.revision or 0)}, mutation_receipt={"receipt_id": "r-priority"}, idempotency_key="k-priority"),
            {},
        ),
        (
            "atom_revision_mismatch",
            _pair_payload(canonical, duplicate, confirmed=True, expected_atom_revisions={canonical.atom_id: int(canonical.revision or 0) + 9, duplicate.atom_id: int(duplicate.revision or 0)}, mutation_receipt={"receipt_id": "r-cas"}, idempotency_key="k-cas"),
            {},
        ),
        (
            "target_not_found",
            {
                "canonical_memory_id": canonical.memory_id,
                "duplicate_memory_id": "5d1a9089cb7dd8ce",
                "confirmed": True,
                "expected_atom_revisions": revisions,
                "mutation_receipt": {"receipt_id": "r-missing"},
                "idempotency_key": "k-missing",
            },
            {},
        ),
        (
            "memory_merge_self_merge_rejected",
            _pair_payload(canonical, canonical, confirmed=True, expected_atom_revisions={canonical.atom_id: int(canonical.revision or 0)}, mutation_receipt={"receipt_id": "r-self"}, idempotency_key="k-self"),
            {},
        ),
    ]
    for code, payload, kwargs in cases:
        result = _dispatch(tmp_path, "memoryguard_memory_merge_safe", payload, **kwargs)
        assert result["ok"] is False, result
        assert result["code"] == code, result
        assert _snapshot(tmp_path) == before
    missing_preview = _dispatch(
        tmp_path,
        "memoryguard_memory_merge_safe_preview",
        {"canonical_memory_id": canonical.memory_id, "duplicate_memory_id": "5d1a9089cb7dd8ce"},
    )
    assert missing_preview["ok"] is False
    assert missing_preview["code"] == "target_not_found"
    assert _snapshot(tmp_path) == before


def test_merge_safe_replay_is_idempotent(tmp_path: Path) -> None:
    canonical = _seed_atom(tmp_path, "memory-canonical", priority=80)
    duplicate = _seed_atom(tmp_path, "3bf-duplicate", agent="admin", priority=10)
    payload = _pair_payload(
        canonical,
        duplicate,
        confirmed=True,
        expected_atom_revisions={
            canonical.atom_id: int(canonical.revision or 0),
            duplicate.atom_id: int(duplicate.revision or 0),
        },
        mutation_receipt={"receipt_id": "receipt-replay"},
        idempotency_key="memory-merge-replay",
    )
    first = _dispatch(tmp_path, "memoryguard_memory_merge_safe", payload)
    assert first["ok"] is True, first
    after = _snapshot(tmp_path)
    second = _dispatch(tmp_path, "memoryguard_memory_merge_safe", payload)
    assert second["ok"] is True, second
    assert second["data"]["decision_id"] == first["data"]["decision_id"]
    assert second["data"].get("idempotent_replay") is True
    assert _snapshot(tmp_path) == after


def test_merge_safe_undo_restores_duplicate(tmp_path: Path) -> None:
    canonical = _seed_atom(tmp_path, "memory-canonical", priority=80)
    duplicate = _seed_atom(tmp_path, "3bf-duplicate", agent="admin", priority=10)
    executed = _dispatch(
        tmp_path,
        "memoryguard_memory_merge_safe",
        _pair_payload(
            canonical,
            duplicate,
            confirmed=True,
            expected_atom_revisions={
                canonical.atom_id: int(canonical.revision or 0),
                duplicate.atom_id: int(duplicate.revision or 0),
            },
            mutation_receipt={"receipt_id": "receipt-undo"},
            idempotency_key="memory-merge-undo",
        ),
    )
    assert executed["ok"] is True, executed
    memory = MemoryAtomStore(tmp_path)
    governance = GovernanceV2(tmp_path, memory_store=memory)
    restored = governance.undo(
        executed["data"]["decision_id"],
        context=V2MutationContext(
            workspace_id=str(tmp_path.resolve()),
            share_group_id=GROUP,
            agent_instance_id="admin",
            project_ref="project-a",
            provider="codex",
            runtime_role="test",
            actor="admin",
            admin=True,
            authority="admin",
        ),
        reason="test undo memory merge",
    )
    assert restored.operation == "undo"
    scope = {"workspace_id": str(tmp_path.resolve()), "share_group_id": GROUP, "admin": True}
    assert memory.get_atom(duplicate.memory_id, scope=scope).status == "active"
    assert memory.get_atom(canonical.memory_id, scope=scope).status == "active"


def test_owner_delete_still_rejects_foreign_canonical(tmp_path: Path) -> None:
    canonical = _seed_atom(tmp_path, "memory-canonical", agent="agent-a", priority=80)
    before = _snapshot(tmp_path)
    result = _dispatch(
        tmp_path,
        "memoryguard_memory_delete",
        {"memory_id": canonical.memory_id, "idempotency_key": "delete-foreign"},
        agent="admin",
    )
    assert result["ok"] is False, result
    assert result["code"] in {"memory_not_found", "v2_governance_rejected"}
    assert _snapshot(tmp_path) == before


def test_rule_merge_safe_still_target_not_found_for_memory_ids(tmp_path: Path) -> None:
    canonical = _seed_atom(tmp_path, "memory-canonical", priority=80)
    duplicate = _seed_atom(tmp_path, "3bf-duplicate", agent="admin", priority=10)
    RuleV2Store(tmp_path)
    before = _snapshot(tmp_path)
    result = _dispatch(
        tmp_path,
        "memoryguard_rule_merge_safe",
        {
            "canonical_source_id": canonical.memory_id,
            "duplicate_source_ids": [duplicate.memory_id],
            "confirmed": True,
            "expected_definition_revisions": {"memory-canonical": 1, "3bf-duplicate": 1},
            "mutation_receipt": {"receipt_id": "rule-merge-memory-ids"},
            "idempotency_key": "rule-merge-memory-ids",
        },
    )
    assert result["ok"] is False, result
    assert result["code"] == "rule_merge_target_not_found"
    assert _snapshot(tmp_path) == before


def test_memory_merge_safe_public_mcp_cutover_contract(tmp_path: Path) -> None:
    name = "memoryguard_memory_merge_safe"
    preview = "memoryguard_memory_merge_safe_preview"
    assert name in MCP_TOOL_NAMES
    assert preview in MCP_TOOL_NAMES
    assert name in MCP_MUTATION_NAMES
    assert preview not in MCP_MUTATION_NAMES
    assert name in _MUTATING_TOOLS
    assert preview not in _MUTATING_TOOLS
    assert {name, preview} <= _V2_MEMORY_MERGE_TOOLS
    tools = {item["name"]: item for item in TOOLS}
    required = set(tools[name]["inputSchema"]["required"])
    assert {"confirmed", "expected_atom_revisions", "mutation_receipt", "idempotency_key"} <= required
    properties = tools[name]["inputSchema"]["properties"]
    assert "canonical_memory_id" in properties
    assert "canonical_atom_id" in properties
    assert "duplicate_memory_id" in properties
    assert "duplicate_atom_id" in properties
    assert "force" not in properties
    assert "bypass" not in properties
    _validate_v2_mcp_arguments(name, {
        "confirmed": True,
        "canonical_memory_id": "memory-canonical",
        "duplicate_memory_id": "3bf-duplicate",
        "expected_atom_revisions": {"atom-a": 4, "atom-b": 2},
        "mutation_receipt": {"receipt_id": "schema-check"},
        "idempotency_key": "schema-check",
    })
    _validate_v2_mcp_arguments(preview, {"canonical_memory_id": "memory-canonical", "duplicate_memory_id": "3bf-duplicate"})
    entries = {
        item["name"]: item
        for item in NativeV2RuntimePort(tmp_path, state_provider=_Manifest()).coverage()["surfaces"]["mcp"]["entries"]
    }
    assert entries[name]["status"] == "implemented"
    assert entries[name]["mutation"] is True
    assert entries[name]["handler"] == "memory_merge_safe"
    assert entries[preview]["status"] == "implemented"
    assert entries[preview]["mutation"] is False
    assert entries[preview]["handler"] == "memory_merge_safe_preview"
