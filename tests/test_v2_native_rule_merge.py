from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.rule_definition import build_definition
from memoryguard.rule_merge_store import RuleMergeStore
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_test_capability,
    bind_native_transport_context,
)
from memoryguard.runtime_v2.rule_merge_native import NativeRuleMergeService


class _Manifest:
    def __init__(self, state: str = "V2_ACTIVE", generation: int = 7):
        self.state = state
        self.generation = generation

    def current(self):
        return {"state": self.state, "generation": self.generation}


def _context(workspace: Path, *, admin: bool = True, agent: str = "agent-a"):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id="session-a",
            session_source="host",
            session_trusted=True,
        ),
        workspace_id=str(workspace),
        share_group_id="group-a",
        project_ref="project-a",
        provider="codex",
    )


def _seed(workspace: Path):
    store = RuleMergeStore(workspace)
    a = build_definition("Always save an audit receipt", kind="procedure")
    b = build_definition("Always preserve an audit receipt", kind="procedure")
    store.upsert_definition(a)
    store.upsert_definition(b)
    proposal = store.create_proposal(
        [a.definition_id, b.definition_id], 0.9,
        definition_a=a, definition_b=b,
    )
    return store, proposal


def _native(workspace: Path, store: RuleMergeStore, manifest: _Manifest | None = None):
    return NativeRuleMergeService(
        workspace,
        rule_store=bind_native_test_capability(rule_merge_store=store),
        state_provider=manifest or _Manifest(),
    )


def _receipt(key: str = "receipt-a"):
    return {"receipt_id": key, "source": "test"}


def _secret(seed: str = "recovery-secret") -> str:
    raw = (seed.encode("utf-8") * 32)[:32]
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_native_rule_merge_rejects_plain_mapping_identity_spoof(tmp_path: Path):
    store, proposal = _seed(tmp_path)
    service = _native(tmp_path, store)
    forged = {
        "workspace_id": str(tmp_path),
        "agent_instance_id": "attacker",
        "share_group_id": "group-a",
        "admin": True,
    }
    result = service.dispatch(
        "memoryguard_rule_merge_capability_issue",
        {"proposal_id": proposal["proposal_id"], "idempotency_key": "issue-1", "mutation_receipt": _receipt()},
        context=forged, generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] is False
    assert result["code"] == "native_trusted_capability_required"
    assert "attacker" not in json.dumps(result)


def test_native_rule_merge_requires_state_provider_and_strict_generation(tmp_path: Path):
    store, proposal = _seed(tmp_path)
    context = _context(tmp_path)
    no_provider = NativeRuleMergeService(
        tmp_path, rule_store=bind_native_test_capability(rule_merge_store=store),
    )
    missing = no_provider.dispatch(
        "issue", {"proposal_id": proposal["proposal_id"], "idempotency_key": "i", "mutation_receipt": _receipt()},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert missing["code"] == "v2_state_provider_required"

    strict = _native(tmp_path, store)
    bad = strict.dispatch(
        "issue", {"proposal_id": proposal["proposal_id"], "idempotency_key": "i2", "mutation_receipt": _receipt()},
        context=context, generation=True, state="V2_ACTIVE",
    )
    assert bad["code"] == "invalid_manifest_generation"


def test_native_rule_merge_production_path_writes_only_v2_rules_db(tmp_path: Path):
    store = RuleV2Store(tmp_path)
    a = store.upsert_definition(build_definition("Always save an audit receipt", kind="procedure"))
    b = store.upsert_definition(build_definition("Always preserve an audit receipt", kind="procedure"))
    proposal_id = "v2-proposal"
    store.record_merge_proposal({
        "proposal_id": proposal_id,
        "definition_ids_json": json.dumps([a.definition_id, b.definition_id]),
        "status": "candidate",
        "metadata_json": json.dumps({"definition_revision_a": a.revision, "definition_revision_b": b.revision}),
    })
    service = NativeRuleMergeService(tmp_path, state_provider=_Manifest())
    context = _context(tmp_path)
    issue = service.dispatch(
        "issue",
        {"proposal_id": proposal_id, "idempotency_key": "v2-issue", "mutation_receipt": _receipt("v2-ri"), "recovery_secret": _secret("v2-secret")},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert issue["ok"] is True
    token = issue["data"]["capability_token"]
    approved = service.dispatch(
        "approve",
        {
            "proposal_id": proposal_id,
            "capability_token": token,
            "expected_definition_revisions": {a.definition_id: a.revision, b.definition_id: b.revision},
            "idempotency_key": "v2-approve",
            "mutation_receipt": _receipt("v2-ra"),
        },
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert approved["ok"] is True
    assert not (tmp_path / ".memoryguard" / "rule-intelligence" / "memory.db").exists()
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT status FROM rule_merge_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()[0] == "approved"
        assert conn.execute("SELECT COUNT(*) FROM rule_governance_capabilities").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM rule_governance_capability_consumptions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM rule_merge_native_requests WHERE status='committed'").fetchone()[0] == 2


def test_native_rule_merge_token_swap_wrong_proposal_and_replay_are_stable(tmp_path: Path):
    store, proposal = _seed(tmp_path)
    # This slice exercises the native transport fence; the Store's independent
    # semantic similarity gate is covered by RuleMergeStore tests.
    store.approve_proposal = lambda *args, **kwargs: {
        "approval_id": "approval-a", "proposal_id": proposal["proposal_id"],
        "approved_by": "agent-a", "capability_id": "hash-a",
    }
    service = _native(tmp_path, store)
    context = _context(tmp_path)
    issue = service.dispatch(
        "issue", {"proposal_id": proposal["proposal_id"], "idempotency_key": "issue-a", "mutation_receipt": _receipt("ri"), "recovery_secret": _secret()},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert issue["ok"] is True
    token = issue["data"]["capability_token"]
    revisions = {
        proposal["definition_ids"][0]: proposal["definition_revision_a"],
        proposal["definition_ids"][1]: proposal["definition_revision_b"],
    }
    missing = service.dispatch(
        "approve", {"proposal_id": proposal["proposal_id"], "capability_token": token, "expected_definition_revisions": revisions, "idempotency_key": "approve-missing",},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert missing["code"] == "mutation_receipt_required"

    approved = service.dispatch(
        "approve", {"proposal_id": proposal["proposal_id"], "capability_token": token, "expected_definition_revisions": revisions, "idempotency_key": "approve-a", "mutation_receipt": _receipt("ra")},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert approved["ok"] is True
    replay = service.dispatch(
        "approve", {"proposal_id": proposal["proposal_id"], "capability_token": token, "expected_definition_revisions": revisions, "idempotency_key": "approve-a", "mutation_receipt": _receipt("ra")},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert replay["ok"] is True and replay["data"].get("idempotent_replay") is True
    conflict = service.dispatch(
        "approve", {"proposal_id": proposal["proposal_id"], "capability_token": "A" * 40, "expected_definition_revisions": revisions, "idempotency_key": "approve-a", "mutation_receipt": _receipt("different")},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert conflict["code"] == "idempotency_conflict"


def test_native_rule_merge_missing_partial_future_db_do_not_write(tmp_path: Path):
    context = _context(tmp_path)
    manifest = _Manifest()
    service = NativeRuleMergeService(tmp_path, state_provider=manifest)
    missing = service.dispatch(
        "issue", {"proposal_id": "missing", "idempotency_key": "missing", "mutation_receipt": _receipt(), "recovery_secret": _secret()},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert missing["code"] == "rule_merge_schema_missing"
    assert not (tmp_path / ".memoryguard").exists()

    v2 = RuleV2Store(tmp_path)
    db = v2.db_path
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE rules_schema_meta SET version=99 WHERE schema_id='rules'")
    changed = db.read_bytes()
    future = service.dispatch(
        "issue", {"proposal_id": "future-proposal", "idempotency_key": "future", "mutation_receipt": _receipt(), "recovery_secret": _secret()},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert future["code"] == "rule_merge_schema_future"
    assert db.read_bytes() == changed


def test_native_rule_merge_restart_replay_is_durable_and_never_reissues_token(tmp_path: Path):
    store, proposal = _seed(tmp_path)
    context = _context(tmp_path)
    first = _native(tmp_path, store).dispatch(
        "issue", {"proposal_id": proposal["proposal_id"], "idempotency_key": "restart-key", "mutation_receipt": _receipt("r"), "recovery_secret": _secret()},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert first["ok"] and first["data"].get("capability_token")
    second = _native(tmp_path, store).dispatch(
        "issue", {"proposal_id": proposal["proposal_id"], "idempotency_key": "restart-key", "mutation_receipt": _receipt("r"), "recovery_secret": _secret()},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert second["ok"] is True
    assert second["data"]["capability_token"] == first["data"]["capability_token"]
    assert second["data"].get("idempotent_replay") is True
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM governance_capabilities").fetchone()[0] == 1


def test_native_rule_merge_same_key_conflict_happens_before_second_write(tmp_path: Path):
    store, proposal = _seed(tmp_path)
    context = _context(tmp_path)
    service = _native(tmp_path, store)
    first = service.dispatch(
        "issue", {"proposal_id": proposal["proposal_id"], "idempotency_key": "conflict-key", "mutation_receipt": _receipt("a"), "recovery_secret": _secret()},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert first["ok"]
    with sqlite3.connect(store.db_path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM governance_capabilities").fetchone()[0]
    conflict = service.dispatch(
        "issue", {"proposal_id": proposal["proposal_id"], "idempotency_key": "conflict-key", "mutation_receipt": _receipt("b"), "recovery_secret": _secret()},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert conflict["code"] == "idempotency_conflict"
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM governance_capabilities").fetchone()[0] == before


def test_native_rule_merge_concurrent_same_key_has_one_durable_effect(tmp_path: Path):
    store, proposal = _seed(tmp_path)
    context = _context(tmp_path)

    def run():
        return _native(tmp_path, store).dispatch(
            "issue", {"proposal_id": proposal["proposal_id"], "idempotency_key": "concurrent-key", "mutation_receipt": _receipt("r"), "recovery_secret": _secret()},
            context=context, generation=7, state="V2_ACTIVE",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert all(item["ok"] for item in results)
    assert results[0]["data"]["capability_token"] == results[1]["data"]["capability_token"]
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM governance_capabilities").fetchone()[0] == 1


def test_native_rule_merge_orphaned_pending_request_fails_closed(tmp_path: Path):
    store, proposal = _seed(tmp_path)
    context = _context(tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO rule_merge_native_requests "
            "(request_key, request_fingerprint, operation, schema_version, status, result_json, created_at, updated_at) "
            "VALUES (?, ?, 'capability_issue', 1, 'pending', '', 'now', 'now')",
            ("orphan-key", "f" * 64),
        )
    result = _native(tmp_path, store).dispatch(
        "issue", {"proposal_id": proposal["proposal_id"], "idempotency_key": "orphan-key", "mutation_receipt": _receipt("orphan"), "recovery_secret": _secret()},
        context=context, generation=7, state="V2_ACTIVE",
    )
    assert result["code"] in {"idempotency_conflict", "idempotency_in_progress"}
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM governance_capabilities").fetchone()[0] == 0


def test_native_rule_merge_non_issue_replays_are_durable_and_token_free(tmp_path: Path):
    store, proposal = _seed(tmp_path)
    context = _context(tmp_path)
    service = _native(tmp_path, store)

    issue_ack = service.dispatch(
        "issue", {"proposal_id": proposal["proposal_id"], "idempotency_key": "issue-ack", "mutation_receipt": _receipt("ia"), "recovery_secret": _secret()},
        context=context, generation=7, state="V2_ACTIVE",
    )
    ack_payload = {
        "proposal_id": proposal["proposal_id"], "capability_token": issue_ack["data"]["capability_token"],
        "idempotency_key": "ack-key", "mutation_receipt": _receipt("ack"),
    }
    ack = service.dispatch("acknowledge", ack_payload, context=context, generation=7, state="V2_ACTIVE")
    assert ack["ok"]
    ack_replay = _native(tmp_path, RuleMergeStore(tmp_path)).dispatch(
        "acknowledge", ack_payload, context=context, generation=7, state="V2_ACTIVE",
    )
    assert ack_replay["ok"] and ack_replay["data"].get("idempotent_replay") is True
    assert "capability_token" not in json.dumps(ack_replay)

    issue_clear = service.dispatch(
        "issue", {"proposal_id": proposal["proposal_id"], "idempotency_key": "issue-clear", "mutation_receipt": _receipt("ic"), "recovery_secret": _secret()},
        context=context, generation=7, state="V2_ACTIVE",
    )
    clear_payload = {
        "proposal_id": proposal["proposal_id"], "capability_token": issue_clear["data"]["capability_token"],
        "idempotency_key": "clear-key", "mutation_receipt": _receipt("clear"),
    }
    clear = service.dispatch("cooldown_clear", clear_payload, context=context, generation=7, state="V2_ACTIVE")
    assert clear["ok"]
    clear_replay = _native(tmp_path, RuleMergeStore(tmp_path)).dispatch(
        "cooldown_clear", clear_payload, context=context, generation=7, state="V2_ACTIVE",
    )
    assert clear_replay["ok"] and clear_replay["data"].get("idempotent_replay") is True
    assert "capability_token" not in json.dumps(clear_replay)


def test_native_rule_merge_recovery_secret_validation_binding_and_no_plaintext(tmp_path: Path):
    store, proposal = _seed(tmp_path)
    context = _context(tmp_path)
    service = _native(tmp_path, store)
    base = {
        "proposal_id": proposal["proposal_id"],
        "idempotency_key": "secret-key",
        "mutation_receipt": _receipt("secret"),
    }
    assert service.dispatch("issue", base, context=context, generation=7, state="V2_ACTIVE")["code"] == "recovery_secret_required"
    assert service.dispatch("issue", {**base, "recovery_secret": "eA"}, context=context, generation=7, state="V2_ACTIVE")["code"] == "recovery_secret_invalid"
    secret = _secret()
    issued = service.dispatch("issue", {**base, "recovery_secret": secret}, context=context, generation=7, state="V2_ACTIVE")
    assert issued["ok"]
    token = issued["data"]["capability_token"]
    wrong = service.dispatch("issue", {**base, "recovery_secret": _secret("different")}, context=context, generation=7, state="V2_ACTIVE")
    assert wrong["code"] == "idempotency_conflict"
    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute("SELECT * FROM rule_merge_native_requests").fetchall()
        capability = conn.execute("SELECT * FROM governance_capabilities").fetchone()
    encoded = json.dumps([tuple(row) for row in rows] + [tuple(capability)])
    assert secret not in encoded and token not in encoded


def test_native_rule_merge_terminal_and_pending_drift_fail_closed(tmp_path: Path):
    store, proposal = _seed(tmp_path)
    context = _context(tmp_path)
    service = _native(tmp_path, store)
    payload = {
        "proposal_id": proposal["proposal_id"],
        "idempotency_key": "terminal-key",
        "mutation_receipt": _receipt("terminal"),
        "recovery_secret": _secret(),
    }
    issued = service.dispatch("issue", payload, context=context, generation=7, state="V2_ACTIVE")
    assert issued["ok"]
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE governance_capabilities SET consumed=1 WHERE proposal_id=?", (proposal["proposal_id"],))
        conn.commit()
    replay = service.dispatch("issue", payload, context=context, generation=7, state="V2_ACTIVE")
    assert replay["code"] == "capability_replay_unavailable"
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE governance_capabilities SET consumed=0")
        conn.execute("UPDATE rule_merge_native_requests SET status='pending', result_json='drift' WHERE request_key=?", ("terminal-key",))
        conn.commit()
    drift = service.dispatch("issue", payload, context=context, generation=7, state="V2_ACTIVE")
    assert drift["code"] == "idempotency_ledger_invalid"


def test_native_rule_merge_registry_activates_all_four_operations(tmp_path: Path):
    entries = {
        item["name"]: item
        for item in NativeV2RuntimePort(tmp_path, state_provider=_Manifest()).coverage()["surfaces"]["mcp"]["entries"]
    }
    names = {
        "memoryguard_rule_merge_capability_issue",
        "memoryguard_rule_merge_approve",
        "memoryguard_rule_merge_acknowledge",
        "memoryguard_rule_merge_cooldown_clear",
    }
    assert all(entries[name]["status"] == "implemented" and entries[name]["mutation"] for name in names)
