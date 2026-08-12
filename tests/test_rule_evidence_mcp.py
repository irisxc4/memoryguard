"""Public MCP evidence/lifecycle regressions over the native V2 store."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.cutover_v2 import V2RuntimeFacade
from memoryguard.mcp_server import handle_request
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store, stable_digest
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.security import feedback_authority


GROUP_ID = "evidence-team"
OWNER_A = "owner-a"
OWNER_B = "owner-b"


@dataclass
class _Manifest:
    state: str = "V2_ACTIVE"
    generation: int = 13

    def current(self) -> dict[str, Any]:
        return {"state": self.state, "generation": self.generation}


def _context(workspace: Path, *, agent: str, project: str | None = None) -> Any:
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id=f"session-{agent}",
            session_source="host",
            session_trusted=True,
        ),
        workspace_id=str(workspace),
        share_group_id=GROUP_ID,
        project_ref=project or str(workspace),
        provider="codex",
        runtime_role="mcp",
    )


def _install_v2(tmp_path: Path, monkeypatch):
    store = RuleV2Store(tmp_path)
    manifest = _Manifest()
    port = NativeV2RuntimePort(tmp_path, state_provider=manifest)
    facade = V2RuntimeFacade(
        manifest=manifest,
        v2=port,
        hook_v2=port,
        workspace=str(tmp_path),
    )
    active = {"context": _context(tmp_path, agent=OWNER_B)}
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(
        "memoryguard.mcp_server._v2_runtime_facade_factory",
        lambda workspace: facade,
    )
    monkeypatch.setattr(
        "memoryguard.mcp_server._trusted_context_for_v2",
        lambda args, workspace: (active["context"], None),
    )
    return store, active


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = handle_request({
        "jsonrpc": "2.0",
        "id": name,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    return response["result"]


def _data(result: dict[str, Any]) -> dict[str, Any]:
    assert result.get("isError") is not True, result
    return json.loads(result["content"][0]["text"])["data"]


def _error(result: dict[str, Any]) -> str:
    assert result.get("isError") is True, result
    payload = json.loads(result["content"][0]["text"])
    return str(payload.get("code") or payload.get("error") or "")


def _seed_receipt(
    store: RuleV2Store,
    *,
    receipt_id: str,
    definition_id: str,
    agent: str = OWNER_B,
) -> None:
    definition = store.get_definition(definition_id)
    if definition is None:
        definition = store.upsert_definition(
            build_definition("run tests before commit", definition_id=definition_id),
        )
    store.record_receipt({
        "receipt_id": receipt_id,
        "definition_id": definition.definition_id,
        "source_rule_id": definition.definition_id,
        "share_group_id": GROUP_ID,
        "agent_instance_id": agent,
        "project_ref": str(store.workspace),
        "session_id": f"session-{agent}",
        "task_hash": f"task-{receipt_id}",
        "selection_digest": "selection",
        "metadata_json": "{}",
        "created_at": "2026-08-12T00:00:00+00:00",
    })


def _submit_feedback(receipt_id: str, outcome: str, *, key: str, evidence: str = "") -> dict[str, Any]:
    return _data(_call("memoryguard_rule_feedback", {
        "receipt_id": receipt_id,
        "outcome": outcome,
        "evidence": evidence,
        "confidence": 1.0,
        "idempotency_key": key,
    }))


def _contribution(store: RuleV2Store, feedback_id: str) -> sqlite3.Row:
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM rule_evidence_contributions WHERE feedback_id=?",
            (feedback_id,),
        ).fetchone()
    assert row is not None
    return row


def test_owner_a_cannot_undo_owner_b_evidence(tmp_path, monkeypatch):
    store, active = _install_v2(tmp_path, monkeypatch)
    definition_id = "owner-b-definition"
    _seed_receipt(store, receipt_id="receipt-b", definition_id=definition_id)
    feedback = _submit_feedback(
        "receipt-b",
        "followed",
        key="feedback-owner-b",
        evidence="owner B evidence",
    )
    winner = _contribution(store, feedback["feedback_id"])
    active["context"] = _context(tmp_path, agent=OWNER_A)
    rejected = _call("memoryguard_rule_undo", {
        "undo_id": feedback["undo_id"],
        "idempotency_key": "undo-owner-a",
    })
    assert _error(rejected) == "rule_undo_owner_mismatch"
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT active FROM rule_evidence_contributions WHERE contribution_id=?",
            (winner["contribution_id"],),
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("winner_outcome", "winner_polarity", "runner_up_polarity"),
    [
        ("followed", "positive", "negative"),
        ("not_applicable", "negative", "positive"),
    ],
)
def test_public_v2_undo_deactivates_evidence_winner_and_restores_runner_up(
    tmp_path,
    monkeypatch,
    winner_outcome: str,
    winner_polarity: str,
    runner_up_polarity: str,
):
    store, _active = _install_v2(tmp_path, monkeypatch)
    definition_id = f"definition-{winner_outcome}"
    _seed_receipt(store, receipt_id="winner-receipt", definition_id=definition_id)
    feedback = _submit_feedback(
        "winner-receipt",
        winner_outcome,
        key=f"feedback-{winner_outcome}",
        evidence="private winner evidence body",
    )
    winner = _contribution(store, feedback["feedback_id"])
    assert winner["polarity"] == winner_polarity

    _seed_receipt(store, receipt_id="runner-up-receipt", definition_id=definition_id)
    runner_up_id = f"runner-up-{winner_outcome}"
    store.record_evidence_contribution({
        "contribution_id": runner_up_id,
        "definition_id": definition_id,
        "independence_key": winner["independence_key"],
        "kind": winner["kind"],
        "polarity": runner_up_polarity,
        "authority": max(0, int(winner["authority"]) - 1),
        "confidence": 1.0,
        "observed_at": "2026-08-12T00:00:01+00:00",
        "active": 1,
        "receipt_id": "runner-up-receipt",
        "feedback_id": "runner-up-feedback",
        "source_evidence_id": "",
        "source_memory_id": definition_id,
        "source_ids_json": json.dumps(["runner-up-receipt"]),
        "metadata_json": "{}",
        "created_at": "2026-08-12T00:00:01+00:00",
        "updated_at": "2026-08-12T00:00:01+00:00",
    })
    store.record_evidence_effective({
        "effective_id": stable_digest(("effective", definition_id, winner["independence_key"])),
        "definition_id": definition_id,
        "independence_key": winner["independence_key"],
        "kind": winner["kind"],
        "winner_contribution_id": winner["contribution_id"],
        "polarity": winner_polarity,
        "authority": int(winner["authority"]),
        "confidence": 1.0,
        "observed_at": str(winner["observed_at"]),
        "updated_at": "2026-08-12T00:00:02+00:00",
    })

    undone = _data(_call("memoryguard_rule_undo", {
        "undo_id": feedback["undo_id"],
        "idempotency_key": f"undo-{winner_outcome}",
    }))
    assert undone["compensation"]["feedback_id"] == feedback["feedback_id"]
    with sqlite3.connect(store.db_path) as conn:
        winner_after = conn.execute(
            "SELECT active FROM rule_evidence_contributions WHERE contribution_id=?",
            (winner["contribution_id"],),
        ).fetchone()[0]
        effective = conn.execute(
            "SELECT winner_contribution_id,polarity FROM rule_evidence_effective WHERE definition_id=? AND independence_key=?",
            (definition_id, winner["independence_key"]),
        ).fetchone()
        dump = "\n".join(
            str(value)
            for table in ("rule_feedback_refs", "rule_evidence_contributions", "rule_evidence_effective")
            for row in conn.execute(f"SELECT * FROM {table}").fetchall()
            for value in row
        )
    assert winner_after == 0
    assert effective == (runner_up_id, runner_up_polarity)
    assert "private winner evidence body" not in dump
