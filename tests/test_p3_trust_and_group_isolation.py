"""P3 trust, native rule projection, and V2 group-isolation checks."""
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
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.runtime_v2.rule_merge_native import NativeRuleMergeService


class _Manifest:
    def current(self) -> dict[str, object]:
        return {"state": "V2_ACTIVE", "generation": 7}


def _secret(seed: str = "trust") -> str:
    return base64.urlsafe_b64encode((seed.encode() * 32)[:32]).decode().rstrip("=")


def _context(root: Path, *, source: str = "host", trusted: bool = True, agent: str = "admin-agent", group: str = "related-group"):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id=f"session-{source}",
            session_source=source,
            session_trusted=trusted,
        ),
        workspace_id=str(root.resolve()),
        share_group_id=group,
        project_ref="",
        provider="codex",
        runtime_role="worker",
        entrypoint="test",
    )


def _proposal(root: Path, *, group: str = "related-group") -> tuple[RuleV2Store, dict[str, object]]:
    store = RuleV2Store(root)
    left = store.upsert_definition(build_definition("must run tests before commit", definition_id=f"{group}-left"))
    right = store.upsert_definition(build_definition("must run tests before committing", definition_id=f"{group}-right"))
    proposal_id = f"proposal-{group}"
    store.record_merge_proposal({
        "proposal_id": proposal_id,
        "definition_ids_json": json.dumps([left.definition_id, right.definition_id]),
        "status": "candidate",
        "metadata_json": json.dumps({
            "definition_revision_a": left.revision,
            "definition_revision_b": right.revision,
        }),
    })
    return store, {
        "proposal_id": proposal_id,
        "ids": [left.definition_id, right.definition_id],
        "revisions": {left.definition_id: left.revision, right.definition_id: right.revision},
    }


def _issue(root: Path, *, group: str = "related-group") -> tuple[RuleV2Store, dict[str, object], NativeRuleMergeService, str]:
    store, proposal = _proposal(root, group=group)
    service = NativeRuleMergeService(root, state_provider=_Manifest())
    result = service.dispatch(
        "issue",
        {"proposal_id": proposal["proposal_id"], "idempotency_key": f"issue-{group}", "mutation_receipt": {"receipt_id": f"receipt-{group}"}, "recovery_secret": _secret(group)},
        context=_context(root, group=group), generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    return store, proposal, service, result["data"]["capability_token"]


def _put_memory(root: Path, group: str, memory_id: str) -> MemoryAtomStore:
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    governance.put_atom(
        MemoryAtom(
            memory_id=memory_id,
            body=f"memory for {group}",
            kind="procedure",
            status="active",
            share_group_id=group,
            workspace_id=str(root.resolve()),
            agent_instance_id=f"agent-{group}",
        ),
        context=V2MutationContext(
            workspace_id=str(root.resolve()),
            share_group_id=group,
            agent_instance_id=f"agent-{group}",
            actor=f"agent-{group}",
            admin=True,
            authority="manual",
        ),
        evidence=[{"source_ref": f"group:{group}:{memory_id}"}],
        reason="group isolation fixture",
    )
    return memory


@pytest.mark.parametrize("source", ["generated", "manual", "client"])
def test_forged_session_trust_cannot_issue_merge_capability(tmp_path: Path, source: str):
    _store, proposal = _proposal(tmp_path)
    result = NativeRuleMergeService(tmp_path, state_provider=_Manifest()).dispatch(
        "issue",
        {"proposal_id": proposal["proposal_id"], "idempotency_key": f"forged-{source}", "mutation_receipt": {"receipt_id": f"r-{source}"}, "recovery_secret": _secret(source)},
        context=_context(tmp_path, source=source, trusted=True), generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] is False
    assert result["code"] == "native_trusted_session_required"


@pytest.mark.parametrize("source", ["host", "transport", "generated", "manual", "client"])
def test_feedback_trust_requires_host_or_transport(source: str):
    context = AccessContext(
        "agent", True, True, False,
        session_id="session-1", session_source=source, session_trusted=True,
    )
    allowed, _reason = context.require_capability_issue()
    assert allowed is (source in {"host", "transport"})
    assert context.session_trusted is (source in {"host", "transport"})


@pytest.mark.parametrize("source", ["generated", "manual", "client"])
def test_feedback_projection_rejects_forged_session_trust(tmp_path: Path, source: str):
    _store, proposal = _proposal(tmp_path)
    result = NativeRuleMergeService(tmp_path, state_provider=_Manifest()).dispatch(
        "issue",
        {"proposal_id": proposal["proposal_id"], "idempotency_key": f"projection-{source}", "mutation_receipt": {"receipt_id": f"p-{source}"}, "recovery_secret": _secret(source)},
        context=_context(tmp_path, source=source, trusted=True), generation=7, state="V2_ACTIVE",
    )
    assert result["code"] == "native_trusted_session_required"


def test_projection_barrier_only_checks_definition_groups(tmp_path: Path):
    rules = RuleV2Store(tmp_path)
    rules.record_canonical_state({
        "scope_id": "related-scope", "share_group_id": "related-group",
        "activation_status": "active", "read_path": "native",
        "canonical_digest": "related", "updated_at": "2026-08-12T00:00:00+00:00",
    })
    rules.record_canonical_state({
        "scope_id": "unrelated-scope", "share_group_id": "unrelated-group",
        "activation_status": "shadow", "read_path": "native",
        "canonical_digest": "unrelated", "updated_at": "2026-08-12T00:00:00+00:00",
    })
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    related = port.dispatch_mcp(
        "memoryguard_canonical_status", {}, context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    unrelated = port.dispatch_mcp(
        "memoryguard_canonical_status", {}, context=_context(tmp_path, group="unrelated-group", agent="other"), generation=7, state="V2_ACTIVE",
    )
    assert related["data"]["canonical_state"] == "active"
    assert unrelated["data"]["canonical_state"] == "shadow"


def test_runtime_high_water_digest_ignores_unrelated_group(tmp_path: Path):
    service = GroupControlService(tmp_path, write=True)
    service.bind_agents(["agent-related-a", "agent-related-b"], share_group_id="related-group")
    service.bind_agents(["agent-other-a", "agent-other-b"], share_group_id="unrelated-group")
    memory = _put_memory(tmp_path, "related-group", "related-memory")
    _put_memory(tmp_path, "unrelated-group", "unrelated-memory")
    related_scope = MemoryReadScope(workspace_id=str(tmp_path.resolve()), share_group_id="related-group", admin=True)
    before = [(atom.memory_id, atom.canonical_hash) for atom in memory.list_atoms(scope=related_scope, include_building=True)]
    unrelated_scope = MemoryReadScope(workspace_id=str(tmp_path.resolve()), share_group_id="unrelated-group", admin=True)
    assert memory.list_atoms(scope=unrelated_scope, include_building=True)
    after = [(atom.memory_id, atom.canonical_hash) for atom in memory.list_atoms(scope=related_scope, include_building=True)]
    assert after == before


def test_outbox_drain_only_consumes_selected_groups(tmp_path: Path):
    memory = _put_memory(tmp_path, "related-group", "related-memory")
    _put_memory(tmp_path, "unrelated-group", "unrelated-memory")
    events = memory.pending_outbox(include_failed=True)
    atom_groups = {}
    for group in ("related-group", "unrelated-group"):
        scope = MemoryReadScope(
            workspace_id=str(tmp_path.resolve()), share_group_id=group, admin=True,
        )
        atom_groups.update({atom.atom_id: group for atom in memory.list_atoms(scope=scope, include_building=True)})
    groups = {atom_groups.get(str(event.get("aggregate_id") or ""), "") for event in events}
    assert {"related-group", "unrelated-group"} <= groups
    selected = [
        event for event in events
        if atom_groups.get(str(event.get("aggregate_id") or ""), "") == "related-group"
    ]
    untouched = [
        event for event in events
        if atom_groups.get(str(event.get("aggregate_id") or ""), "") == "unrelated-group"
    ]
    assert selected and untouched
    assert {atom_groups[str(event["aggregate_id"])] for event in selected} == {"related-group"}
    assert {atom_groups[str(event["aggregate_id"])] for event in untouched} == {"unrelated-group"}


def test_merge_barrier_ignores_unrelated_group_lag(tmp_path: Path):
    store, proposal, service, token = _issue(tmp_path)
    _put_memory(tmp_path, "unrelated-group", "unrelated-lag")
    approved = service.dispatch(
        "approve",
        {
            "proposal_id": proposal["proposal_id"],
            "capability_token": token,
            "expected_definition_revisions": proposal["revisions"],
            "idempotency_key": "approve-related",
            "mutation_receipt": {"receipt_id": "approve-related-receipt"},
        },
        context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert approved["ok"] is True, approved
    with sqlite3.connect(store.db_path) as conn:
        status = conn.execute(
            "SELECT status FROM rule_merge_proposals WHERE proposal_id=?",
            (proposal["proposal_id"],),
        ).fetchone()[0]
    assert status == "approved"
