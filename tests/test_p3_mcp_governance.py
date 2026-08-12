from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memoryguard.access_context import AccessContext
from memoryguard.cutover_v2 import V2RuntimeFacade
from memoryguard.mcp_server import handle_request
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)


GROUP_ID = "mcp-group"
AGENT_ID = "mcp-agent"


@dataclass
class _Manifest:
    state: str = "V2_ACTIVE"
    generation: int = 7

    def current(self) -> dict[str, Any]:
        return {"state": self.state, "generation": self.generation}


class _RecordingContextEngine:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def bootstrap(self, request: Any, candidates: Any = None) -> dict[str, Any]:
        del candidates
        value = dict(request)
        self.requests.append(value)
        return {
            "request": value,
            "packet": {
                "mandatory": [],
                "relevant": [],
                "knowledge": [],
                "reference_only": [],
            },
        }


def _secret(seed: str) -> str:
    raw = (seed.encode("utf-8") * 32)[:32]
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _context(
    workspace: Path,
    *,
    session_source: str = "host",
    session_trusted: bool = True,
    admin: bool = False,
) -> Any:
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=AGENT_ID,
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id="host-session-1",
            session_source=session_source,
            session_trusted=session_trusted,
        ),
        workspace_id=str(workspace),
        share_group_id=GROUP_ID,
        project_ref=str(workspace),
        provider="codex",
        runtime_role="mcp",
    )


def _install_v2(tmp_path: Path, monkeypatch, *, admin: bool = False, session_source: str = "host", session_trusted: bool = True):
    manifest = _Manifest()
    engine = _RecordingContextEngine()
    port = NativeV2RuntimePort(
        tmp_path,
        context_engine=engine,
        state_provider=manifest,
    )
    facade = V2RuntimeFacade(
        manifest=manifest,
        v2=port,
        hook_v2=port,
        workspace=str(tmp_path),
    )
    context = _context(
        tmp_path,
        session_source=session_source,
        session_trusted=session_trusted,
        admin=admin,
    )
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(
        "memoryguard.mcp_server._v2_runtime_facade_factory",
        lambda workspace: facade,
    )
    monkeypatch.setattr(
        "memoryguard.mcp_server._trusted_context_for_v2",
        lambda args, workspace: (context, None),
    )
    return RuleV2Store(tmp_path), engine


def _packet(result: dict[str, Any]) -> dict[str, Any]:
    assert result.get("isError") is not True, result
    return json.loads(result["content"][0]["text"])


def _mcp_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = handle_request({
        "jsonrpc": "2.0",
        "id": name,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert response is not None
    assert response["id"] == name
    return response["result"]


def _seed_receipt(store: RuleV2Store, *, receipt_id: str = "receipt-v2") -> str:
    definition = store.upsert_definition(
        build_definition("Always run tests before commit", definition_id="v2-rule"),
    )
    store.record_receipt({
        "receipt_id": receipt_id,
        "definition_id": definition.definition_id,
        "source_rule_id": definition.definition_id,
        "share_group_id": GROUP_ID,
        "agent_instance_id": AGENT_ID,
        "project_ref": str(store.workspace),
        "session_id": "host-session-1",
        "task_hash": "task-hash",
        "selection_digest": "selection",
        "metadata_json": "{}",
        "created_at": "2026-08-12T00:00:00+00:00",
    })
    return receipt_id


def test_mcp_bootstrap_and_feedback_preserve_v2_host_provenance(tmp_path, monkeypatch):
    store, engine = _install_v2(tmp_path, monkeypatch)
    receipt_id = _seed_receipt(store)

    packet = _packet(_mcp_call(
        "memoryguard_context_bootstrap",
        {"workspace": str(tmp_path), "task": "run tests before commit", "max_tokens": 128},
    ))
    request = packet["data"]["request"]
    assert request["agent_instance_id"] == AGENT_ID
    assert request["share_group_id"] == GROUP_ID
    assert request["provider"] == "codex"
    assert request["max_tokens"] == 128
    assert engine.requests[-1]["trusted_identity"]

    feedback = _packet(_mcp_call("memoryguard_rule_feedback", {
        "workspace": str(tmp_path),
        "receipt_id": receipt_id,
        "outcome": "followed",
        "actor": "ignored-client-actor",
        "idempotency_key": "feedback-host-session",
    }))
    assert feedback["data"]["outcome"] == "followed"
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            "SELECT metadata_json FROM rule_feedback_refs WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
    assert row is not None
    metadata = json.loads(row[0])
    assert metadata["session_id"] == "host-session-1"
    assert metadata["session_source"] == "host"


def test_v2_feedback_without_trusted_source_remains_untrusted(tmp_path, monkeypatch):
    store, _engine = _install_v2(
        tmp_path,
        monkeypatch,
        session_source="",
        session_trusted=False,
    )
    receipt_id = _seed_receipt(store, receipt_id="receipt-untrusted")
    result = _packet(_mcp_call("memoryguard_rule_feedback", {
        "workspace": str(tmp_path),
        "receipt_id": receipt_id,
        "outcome": "followed",
        "idempotency_key": "feedback-untrusted",
    }))
    assert result["data"]["outcome"] == "followed"
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            "SELECT metadata_json FROM rule_feedback_refs WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
    assert row is not None
    metadata = json.loads(row[0])
    # V2 records the normalized provenance source; it intentionally does not
    # duplicate the trust bit in the feedback row.  An absent source is the
    # stable fail-closed representation for an untrusted session.
    assert metadata["session_source"] == "absent"
    assert "session_trusted" not in metadata


def _candidate(store: RuleV2Store, suffix: str, *, cooldown_until: str = "") -> dict[str, Any]:
    left = store.upsert_definition(
        build_definition(
            f"must run tests before commit {suffix}",
            definition_id=f"{suffix}-left",
        ),
    )
    right = store.upsert_definition(
        build_definition(
            f"must run tests before commit {suffix}",
            definition_id=f"{suffix}-right",
        ),
    )
    proposal_id = f"{suffix}-proposal"
    store.record_merge_proposal({
        "proposal_id": proposal_id,
        "definition_ids_json": json.dumps([left.definition_id, right.definition_id]),
        "status": "candidate",
        "metadata_json": json.dumps({
            "definition_revision_a": left.revision,
            "definition_revision_b": right.revision,
            "cooldown_until": cooldown_until,
        }),
    })
    return {
        "proposal_id": proposal_id,
        "definition_ids": [left.definition_id, right.definition_id],
        "expected_definition_revisions": {
            left.definition_id: left.revision,
            right.definition_id: right.revision,
        },
    }


def _issue_token(tmp_path: Path, proposal_id: str, suffix: str) -> str:
    payload = _packet(_mcp_call(
        "memoryguard_rule_merge_capability_issue",
        {
            "workspace": str(tmp_path),
            "proposal_id": proposal_id,
            "idempotency_key": f"issue-{suffix}",
            "mutation_receipt": {"receipt_id": f"receipt-issue-{suffix}"},
            "recovery_secret": _secret(f"recovery-{suffix}"),
        },
    ))
    token = payload["data"]["capability_token"]
    assert token and not token.startswith("admin:")
    return token


def test_mcp_v2_merge_handlers_issue_approve_ack_and_clear_cooldown(
    tmp_path,
    monkeypatch,
):
    store, _engine = _install_v2(tmp_path, monkeypatch, admin=True)

    approval = _candidate(store, "approval")
    token = _issue_token(tmp_path, approval["proposal_id"], "approval")
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            "SELECT token_digest,metadata_json FROM rule_governance_capabilities WHERE proposal_id=?",
            (approval["proposal_id"],),
        ).fetchone()
    assert row is not None
    assert token not in json.dumps(row)

    approved = _packet(_mcp_call("memoryguard_rule_merge_approve", {
        "workspace": str(tmp_path),
        "proposal_id": approval["proposal_id"],
        "capability_token": token,
        "expected_definition_revisions": approval["expected_definition_revisions"],
        "idempotency_key": "approve-approval",
        "mutation_receipt": {"receipt_id": "receipt-approve-approval"},
    }))
    assert approved["data"]["proposal_id"] == approval["proposal_id"]
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT status FROM rule_merge_proposals WHERE proposal_id=?",
            (approval["proposal_id"],),
        ).fetchone()[0] == "approved"

    acknowledged = _candidate(store, "acknowledge")
    ack_token = _issue_token(tmp_path, acknowledged["proposal_id"], "acknowledge")
    ack_result = _packet(_mcp_call("memoryguard_rule_merge_acknowledge", {
        "workspace": str(tmp_path),
        "proposal_id": acknowledged["proposal_id"],
        "capability_token": ack_token,
        "idempotency_key": "ack-acknowledge",
        "mutation_receipt": {"receipt_id": "receipt-ack-acknowledge"},
    }))
    assert ack_result["data"]["metadata"]["first_merge_acknowledged"] is True

    cooldown = _candidate(
        store,
        "cooldown",
        cooldown_until="2099-01-01T00:00:00+00:00",
    )
    cooldown_token = _issue_token(tmp_path, cooldown["proposal_id"], "cooldown")
    clear_result = _packet(_mcp_call("memoryguard_rule_merge_cooldown_clear", {
        "workspace": str(tmp_path),
        "proposal_id": cooldown["proposal_id"],
        "capability_token": cooldown_token,
        "idempotency_key": "clear-cooldown",
        "mutation_receipt": {"receipt_id": "receipt-clear-cooldown"},
    }))
    assert clear_result["data"]["metadata"]["cooldown_until"] == ""
