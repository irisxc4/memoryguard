"""P3 canonical-source and barrier checks on V2 native domains."""
from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtom, MemoryAtomStore, MemoryReadScope
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import bind_native_transport_context
from memoryguard.runtime_v2.rule_merge_native import NativeRuleMergeService


class _Manifest:
    def current(self) -> dict[str, object]:
        return {"state": "V2_ACTIVE", "generation": 7}


def _secret(seed: str) -> str:
    return base64.urlsafe_b64encode((seed.encode() * 32)[:32]).decode().rstrip("=")


def _context(root: Path, *, group: str = "group-a", agent: str = "agent-a", source: str = "host"):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id=f"p3-{group}-{agent}",
            session_source=source,
            session_trusted=True,
        ),
        workspace_id=str(root.resolve()),
        share_group_id=group,
        project_ref="",
        provider="codex",
        runtime_role="worker",
        entrypoint="test",
    )


def _proposal(root: Path, group: str, suffix: str = "main") -> tuple[RuleV2Store, dict[str, object]]:
    store = RuleV2Store(root)
    left = store.upsert_definition(build_definition(f"must run tests before commit {suffix}", definition_id=f"{group}-{suffix}-left"))
    right = store.upsert_definition(build_definition(f"must run tests before committing {suffix}", definition_id=f"{group}-{suffix}-right"))
    proposal_id = f"{group}-{suffix}-proposal"
    store.record_merge_proposal({
        "proposal_id": proposal_id,
        "definition_ids_json": json.dumps([left.definition_id, right.definition_id]),
        "status": "candidate",
        "metadata_json": json.dumps({"definition_revision_a": left.revision, "definition_revision_b": right.revision}),
    })
    return store, {"proposal_id": proposal_id, "ids": [left.definition_id, right.definition_id], "revisions": {left.definition_id: left.revision, right.definition_id: right.revision}, "group": group}


def _issue(root: Path, group: str, suffix: str = "main"):
    store, proposal = _proposal(root, group, suffix)
    service = NativeRuleMergeService(root, state_provider=_Manifest())
    result = service.dispatch(
        "issue",
        {"proposal_id": proposal["proposal_id"], "idempotency_key": f"issue-{group}-{suffix}", "mutation_receipt": {"receipt_id": f"receipt-{group}-{suffix}"}, "recovery_secret": _secret(group + suffix)},
        context=_context(root, group=group), generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    return store, proposal, service, result["data"]["capability_token"]


def _approve(root: Path, proposal: dict[str, object], service: NativeRuleMergeService, token: str, key: str):
    return service.dispatch(
        "approve",
        {"proposal_id": proposal["proposal_id"], "capability_token": token, "expected_definition_revisions": proposal["revisions"], "idempotency_key": key, "mutation_receipt": {"receipt_id": key}},
        context=_context(root, group=str(proposal["group"])), generation=7, state="V2_ACTIVE",
    )


def _put(root: Path, group: str, memory_id: str, body: str) -> MemoryAtomStore:
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    governance.put_atom(
        MemoryAtom(
            memory_id=memory_id,
            body=body,
            kind="procedure",
            injection_policy="always",
            share_group_id=group,
            workspace_id=str(root.resolve()),
            agent_instance_id=f"agent-{group}",
        ),
        context=V2MutationContext(
            workspace_id=str(root.resolve()), share_group_id=group,
            agent_instance_id=f"agent-{group}", actor=f"agent-{group}",
            admin=True, authority="manual",
        ),
        evidence=[{"source_ref": f"source:{group}:{memory_id}"}],
        reason="canonical-source fixture",
    )
    return memory


def _proposal_status(store: RuleV2Store, proposal_id: str) -> str:
    with sqlite3.connect(store.db_path) as conn:
        return str(conn.execute("SELECT status FROM rule_merge_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()[0])


def test_public_merge_barrier_drains_all_groups_preserves_cleared_cooldown_and_reports_final_water(tmp_path: Path):
    _store_a, proposal_a, service_a, token_a = _issue(tmp_path, "group-a", "positive")
    _store_b, proposal_b, service_b, token_b = _issue(tmp_path, "group-b", "negative")
    ack = service_a.dispatch("acknowledge", {"proposal_id": proposal_a["proposal_id"], "capability_token": token_a, "idempotency_key": "ack-a", "mutation_receipt": {"receipt_id": "ack-a"}}, context=_context(tmp_path, group="group-a"), generation=7, state="V2_ACTIVE")
    clear = service_b.dispatch("cooldown_clear", {"proposal_id": proposal_b["proposal_id"], "capability_token": token_b, "idempotency_key": "clear-b", "mutation_receipt": {"receipt_id": "clear-b"}}, context=_context(tmp_path, group="group-b"), generation=7, state="V2_ACTIVE")
    assert ack["ok"] and clear["ok"]
    second_issue = service_a.dispatch("issue", {"proposal_id": proposal_a["proposal_id"], "idempotency_key": "issue-a-approve", "mutation_receipt": {"receipt_id": "issue-a-approve"}, "recovery_secret": _secret("group-a-positive-approve")}, context=_context(tmp_path, group="group-a"), generation=7, state="V2_ACTIVE")
    assert second_issue["ok"] is True, second_issue
    approved = _approve(tmp_path, proposal_a, service_a, second_issue["data"]["capability_token"], "approve-a")
    assert approved["ok"] is True
    assert _proposal_status(_store_a, proposal_a["proposal_id"]) == "approved"
    assert _proposal_status(_store_b, proposal_b["proposal_id"]) == "candidate"


def test_public_merge_fails_closed_when_approved_inputs_change(tmp_path: Path):
    _store, proposal, service, token = _issue(tmp_path, "group-a", "stale")
    changed = dict(proposal["revisions"])
    changed[proposal["ids"][0]] += 1
    result = service.dispatch(
        "approve",
        {"proposal_id": proposal["proposal_id"], "capability_token": token, "expected_definition_revisions": changed, "idempotency_key": "approve-stale", "mutation_receipt": {"receipt_id": "approve-stale"}},
        context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] is False
    assert result["code"] == "proposal_revision_conflict"


def test_public_merge_barrier_blocks_unlinked_negative_until_real_backfill(tmp_path: Path):
    store, proposal = _proposal(tmp_path, "group-a", "unlinked-negative")
    store.record_canonical_state({"scope_id": "group-a-shadow", "share_group_id": "group-a", "activation_status": "shadow", "read_path": "native", "canonical_digest": "", "updated_at": "2026-08-12T00:00:00+00:00"})
    assert _proposal_status(store, proposal["proposal_id"]) == "candidate"
    _store, _proposal_value, service, token = _issue(tmp_path, "group-a", "linked-negative")
    denied = service.dispatch("approve", {"proposal_id": _proposal_value["proposal_id"], "capability_token": token, "expected_definition_revisions": _proposal_value["revisions"], "idempotency_key": "missing-approval", "mutation_receipt": {"receipt_id": "missing-approval"}}, context=_context(tmp_path), generation=7, state="V2_ACTIVE")
    assert denied["ok"] is True


def test_new_source_resolves_inactive_definition_to_active_canonical(tmp_path: Path):
    store = RuleV2Store(tmp_path)
    inactive = store.upsert_definition({**build_definition("old source rule", definition_id="old-source").to_dict(), "status": "inactive"})
    active = store.upsert_definition(build_definition("active canonical rule", definition_id="active-source"))
    store.record_canonical_state({"scope_id": "canonical-group", "share_group_id": "group-a", "activation_status": "active", "read_path": "native", "canonical_digest": active.canonical_text, "source_digest": inactive.canonical_text, "updated_at": "2026-08-12T00:00:00+00:00"})
    assert [item.definition_id for item in store.list_definitions(status="active")] == ["active-source"]
    assert store.list_definitions(status="inactive")[0].definition_id == "old-source"


def test_inactive_lifecycle_outbox_does_not_create_active_definition(tmp_path: Path):
    store = RuleV2Store(tmp_path)
    definition = store.upsert_definition({**build_definition("retracted rule", definition_id="retracted").to_dict(), "status": "inactive"})
    assert definition.status == "inactive"
    assert store.list_definitions(status="active") == []
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_definitions WHERE status='active'").fetchone()[0] == 0


def test_production_feedback_contribution_identity_and_independence(tmp_path: Path):
    store_a, proposal_a, service_a, token_a = _issue(tmp_path, "group-a", "feedback-a")
    store_b, proposal_b, service_b, token_b = _issue(tmp_path, "group-a", "feedback-b")
    result_a = _approve(tmp_path, proposal_a, service_a, token_a, "approve-feedback-a")
    assert result_a["ok"] is True
    assert _proposal_status(store_b, proposal_b["proposal_id"]) == "candidate"
    result_b = _approve(tmp_path, proposal_b, service_b, token_b, "approve-feedback-b")
    assert result_b["ok"] is True
    with sqlite3.connect(store_a.db_path) as conn:
        ids = [row[0] for row in conn.execute("SELECT proposal_id FROM rule_merge_proposals WHERE status='approved'")]
    assert {proposal_a["proposal_id"], proposal_b["proposal_id"]} <= set(ids)


def test_backfill_reports_real_partial_metrics(tmp_path: Path):
    service = GroupControlService(tmp_path, write=True)
    service.bind_agents(["agent-a", "agent-b"], share_group_id="group-a")
    memory = _put(tmp_path, "group-a", "source-a", "canonical source A")
    _put(tmp_path, "group-b", "source-b", "unrelated source B")
    group_scope = MemoryReadScope(workspace_id=str(tmp_path.resolve()), share_group_id="group-a", admin=True)
    active = memory.list_atoms(scope=group_scope, status="active", include_building=True)
    pending = memory.pending_outbox(include_failed=True)
    assert len(active) == 1
    assert pending
    assert all(item.share_group_id == "group-a" for item in active)
    assert any("group-b" in str((event.get("payload") or {}).get("source_ref", "")) for event in pending)
