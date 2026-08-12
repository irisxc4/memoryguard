"""Final P3 regression coverage against the V2 native rule boundary."""
from __future__ import annotations

import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
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
    resolve_native_transport_context,
)
from memoryguard.runtime_v2.rule_merge_native import NativeRuleMergeService


class _Manifest:
    def __init__(self, state: str = "V2_ACTIVE", generation: int = 7) -> None:
        self.state = state
        self.generation = generation

    def current(self) -> dict[str, object]:
        return {"state": self.state, "generation": self.generation}


def _secret(seed: str = "final") -> str:
    return base64.urlsafe_b64encode((seed.encode() * 32)[:32]).decode().rstrip("=")


def _context(root: Path, *, agent: str = "admin-agent", group: str = "group-a", source: str = "host", trusted: bool = True):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id=f"session-{agent}-{source}",
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


def _proposal(root: Path, *, proposal_id: str = "p3-proposal", group: str = "group-a") -> tuple[RuleV2Store, dict[str, object]]:
    store = RuleV2Store(root)
    left = store.upsert_definition(build_definition("Always preserve an audit receipt", kind="procedure", definition_id=f"{proposal_id}-a"))
    right = store.upsert_definition(build_definition("Always save an audit receipt", kind="procedure", definition_id=f"{proposal_id}-b"))
    store.record_merge_proposal({
        "proposal_id": proposal_id,
        "definition_ids_json": json.dumps([left.definition_id, right.definition_id]),
        "status": "candidate",
        "metadata_json": json.dumps({"definition_revision_a": left.revision, "definition_revision_b": right.revision}),
    })
    return store, {
        "proposal_id": proposal_id,
        "ids": [left.definition_id, right.definition_id],
        "revisions": {left.definition_id: left.revision, right.definition_id: right.revision},
        "group": group,
    }


def _issue(root: Path, *, proposal_id: str = "p3-proposal", key: str = "issue-p3", group: str = "group-a"):
    store, proposal = _proposal(root, proposal_id=proposal_id, group=group)
    service = NativeRuleMergeService(root, state_provider=_Manifest())
    result = service.dispatch(
        "issue",
        {"proposal_id": proposal_id, "idempotency_key": key, "mutation_receipt": {"receipt_id": f"receipt-{key}"}, "recovery_secret": _secret(key)},
        context=_context(root, group=group), generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    return store, proposal, service, result["data"]["capability_token"]


def _approve(root: Path, proposal: dict[str, object], service: NativeRuleMergeService, token: str, *, key: str = "approve-p3"):
    return service.dispatch(
        "approve",
        {
            "proposal_id": proposal["proposal_id"],
            "capability_token": token,
            "expected_definition_revisions": proposal["revisions"],
            "idempotency_key": key,
            "mutation_receipt": {"receipt_id": f"receipt-{key}"},
        },
        context=_context(root, group=str(proposal["group"])), generation=7, state="V2_ACTIVE",
    )


def test_forged_admin_prefix_cannot_approve(tmp_path: Path):
    _store, proposal, service, token = _issue(tmp_path)
    forged = {
        "workspace_id": str(tmp_path),
        "agent_instance_id": "admin-agent",
        "share_group_id": "group-a",
        "admin": True,
    }
    result = service.dispatch(
        "approve",
        {"proposal_id": proposal["proposal_id"], "capability_token": token, "expected_definition_revisions": proposal["revisions"], "idempotency_key": "forged-approve", "mutation_receipt": {"receipt_id": "forged"}},
        context=forged, generation=7, state="V2_ACTIVE",
    )
    assert result["code"] == "native_trusted_capability_required"


def test_first_merge_acknowledgment_requires_capability(tmp_path: Path):
    _store, proposal, service, token = _issue(tmp_path, key="issue-ack")
    missing = service.dispatch(
        "acknowledge",
        {"proposal_id": proposal["proposal_id"], "idempotency_key": "ack-missing", "mutation_receipt": {"receipt_id": "ack-missing"}},
        context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert missing["code"] in {"capability_token_required", "invalid_capability_token"}
    acknowledged = service.dispatch(
        "acknowledge",
        {"proposal_id": proposal["proposal_id"], "capability_token": token, "idempotency_key": "ack-ok", "mutation_receipt": {"receipt_id": "ack-ok"}},
        context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert acknowledged["ok"] is True


def test_cooldown_clear_requires_capability(tmp_path: Path):
    _store, proposal, service, token = _issue(tmp_path, key="issue-clear")
    missing = service.dispatch(
        "cooldown_clear",
        {"proposal_id": proposal["proposal_id"], "idempotency_key": "clear-missing", "mutation_receipt": {"receipt_id": "clear-missing"}},
        context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert missing["code"] in {"capability_token_required", "invalid_capability_token"}
    cleared = service.dispatch(
        "cooldown_clear",
        {"proposal_id": proposal["proposal_id"], "capability_token": token, "idempotency_key": "clear-ok", "mutation_receipt": {"receipt_id": "clear-ok"}},
        context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert cleared["ok"] is True


def test_definition_core_change_creates_new_definition(tmp_path: Path):
    store = RuleV2Store(tmp_path)
    first = store.upsert_definition(build_definition("always run focused tests", definition_id="stable-a"))
    second = store.upsert_definition(build_definition("always run full tests", definition_id="stable-b"))
    assert first.definition_id != second.definition_id
    assert first.canonical_text != second.canonical_text
    assert {item.definition_id for item in store.list_definitions()} >= {"stable-a", "stable-b"}


def test_same_definition_id_immutable_core_change_is_rejected(tmp_path: Path):
    store = RuleV2Store(tmp_path)
    store.upsert_definition(build_definition("always preserve the receipt", definition_id="immutable"))
    with pytest.raises((ValueError, RuntimeError)):
        store.upsert_definition(build_definition("always discard the receipt", definition_id="immutable"))


def test_merge_transaction_recomputes_similarity_from_current_definitions(tmp_path: Path):
    store, proposal, service, token = _issue(tmp_path, key="issue-stale")
    stale_revisions = dict(proposal["revisions"])
    stale_revisions[proposal["ids"][0]] += 1
    result = service.dispatch(
        "approve",
        {
            "proposal_id": proposal["proposal_id"],
            "capability_token": token,
            "expected_definition_revisions": stale_revisions,
            "idempotency_key": "approve-stale",
            "mutation_receipt": {"receipt_id": "approve-stale"},
        },
        context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] is False
    assert result["code"] in {"definition_revision_mismatch", "proposal_revision_stale", "proposal_revision_conflict", "rule_merge_proposal_stale", "definition_revision_required"}


def test_unrelated_projection_update_does_not_block_undo(tmp_path: Path):
    store, proposal, service, token = _issue(tmp_path, key="issue-related")
    rules = RuleV2Store(tmp_path)
    rules.record_canonical_state({
        "scope_id": "unrelated", "share_group_id": "other-group",
        "activation_status": "active", "read_path": "native",
        "canonical_digest": "other", "updated_at": "2026-08-12T00:00:00+00:00",
    })
    approved = _approve(tmp_path, proposal, service, token, key="approve-related")
    assert approved["ok"] is True
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT status FROM rule_merge_proposals WHERE proposal_id=?", (proposal["proposal_id"],)).fetchone()[0] == "approved"


def test_validated_requires_distinct_trusted_sessions_agents_and_projects(tmp_path: Path):
    contexts = [
        _context(tmp_path, agent="agent-a"),
        _context(tmp_path, agent="agent-b"),
        _context(tmp_path, agent="agent-c"),
    ]
    authorities = [resolve_native_transport_context(context).to_dict() for context in contexts]
    assert len({item["agent_instance_id"] for item in authorities}) == 3
    assert all(item["session_trusted"] is True for item in authorities)
    assert len({item["session_id"] for item in authorities}) == 3


def test_concurrent_legacy_feedback_is_ordered_before_merge_commit(tmp_path: Path):
    _store, proposal, _service, _token = _issue(tmp_path, key="issue-concurrent")
    context = _context(tmp_path)

    def issue_again():
        return NativeRuleMergeService(tmp_path, state_provider=_Manifest()).dispatch(
            "issue",
            {"proposal_id": proposal["proposal_id"], "idempotency_key": "concurrent", "mutation_receipt": {"receipt_id": "same"}, "recovery_secret": _secret("concurrent")},
            context=context, generation=7, state="V2_ACTIVE",
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _index: issue_again(), range(3)))
    assert all(result["ok"] for result in results)
    assert len({result["data"]["capability_token"] for result in results}) == 1


def test_global_projection_barrier_serializes_merge_commits(tmp_path: Path):
    _store, proposal, service, token = _issue(tmp_path, key="issue-barrier")
    first = _approve(tmp_path, proposal, service, token, key="approve-barrier")
    replay = _approve(tmp_path, proposal, NativeRuleMergeService(tmp_path, state_provider=_Manifest()), token, key="approve-barrier")
    assert first["ok"] is True
    assert replay["ok"] is True and replay["data"].get("idempotent_replay") is True


def test_canonical_read_falls_back_when_projection_lags(tmp_path: Path):
    rules = RuleV2Store(tmp_path)
    rules.record_canonical_state({
        "scope_id": "canonical", "share_group_id": "group-a",
        "activation_status": "active", "read_path": "native",
        "canonical_digest": "canonical", "updated_at": "2026-08-12T00:00:00+00:00",
    })
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    context = _context(tmp_path)
    canonical = port.dispatch_mcp("memoryguard_canonical_status", {}, context=context, generation=7, state="V2_ACTIVE")
    projection = port.dispatch_mcp("memoryguard_projection_status", {}, context=context, generation=7, state="V2_ACTIVE")
    assert canonical["data"]["canonical_state"] == "active"
    assert projection["data"]["status"] == "NO_SOURCE"
    assert projection["data"]["total_heads"] == 0


def test_active_binding_always_has_active_contribution(tmp_path: Path):
    service = GroupControlService(tmp_path, write=True)
    bound = service.bind_agents(["agent-a", "agent-b"], share_group_id="group-a")
    assert bound["member_count"] == 2
    active = service.list_bindings(include_inactive=False)["bindings"]
    assert {item["agent_instance_id"] for item in active} == {"agent-a", "agent-b"}
    service.commit_governance("group-a", reason="P3 binding check", trusted={"workspace_id": str(tmp_path.resolve()), "agent_instance_id": "agent-a", "share_group_id": "group-a", "project_ref": "project-a", "provider": "codex", "runtime_role": "worker"})
    assert all(item["status"] == "active" for item in service.list_bindings(include_inactive=False)["bindings"])


def test_runner_up_restores_for_positive_and_negative_polarity(tmp_path: Path):
    _store, first, service, token = _issue(tmp_path, proposal_id="runner-positive", key="issue-positive")
    approved = _approve(tmp_path, first, service, token, key="approve-positive")
    assert approved["ok"] is True
    _store2, second, service2, token2 = _issue(tmp_path, proposal_id="runner-negative", key="issue-negative")
    cleared = service2.dispatch(
        "cooldown_clear",
        {"proposal_id": second["proposal_id"], "capability_token": token2, "idempotency_key": "clear-negative", "mutation_receipt": {"receipt_id": "clear-negative"}},
        context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert cleared["ok"] is True
    with sqlite3.connect(_store2.db_path) as conn:
        statuses = {row[0] for row in conn.execute("SELECT status FROM rule_merge_proposals WHERE proposal_id IN (?,?)", (first["proposal_id"], second["proposal_id"]))}
    assert "approved" in statuses and "candidate" in statuses
