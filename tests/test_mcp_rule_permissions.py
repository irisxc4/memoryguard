"""Public MCP authorization regressions against the native V2 planes."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memoryguard.access_context import AccessContext
from memoryguard.cutover_v2 import V2RuntimeFacade
from memoryguard.evidence.store import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.mcp_server import handle_request
from memoryguard.memory.store import MemoryAtomStore
from memoryguard.rule_definition import build_definition
from memoryguard.rule_scope import canonical_project_ref
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)


GROUP_ID = "team"
AGENT_ID = "a"


@dataclass
class _Manifest:
    state: str = "V2_ACTIVE"
    generation: int = 11

    def current(self) -> dict[str, Any]:
        return {"state": self.state, "generation": self.generation}


def _context(
    workspace: Path,
    *,
    agent: str = AGENT_ID,
    group: str = GROUP_ID,
    project: str | None = None,
    admin: bool = False,
    session_source: str = "transport",
    session_trusted: bool = True,
) -> Any:
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id=f"session-{agent}",
            session_source=session_source,
            session_trusted=session_trusted,
        ),
        workspace_id=str(workspace),
        share_group_id=group,
        project_ref=project or str(workspace),
        provider="codex",
        runtime_role="root",
    )


def _install_v2(tmp_path: Path, monkeypatch):
    # These are all V2 stores.  Creating them up front also makes the public
    # port's schema preflight observable instead of silently provisioning a
    # compatibility database during a test.
    memory = MemoryAtomStore(tmp_path)
    EvidenceStore(tmp_path)
    GovernanceV2(tmp_path)
    RuleV2Store(tmp_path)
    manifest = _Manifest()
    port = NativeV2RuntimePort(tmp_path, state_provider=manifest)
    facade = V2RuntimeFacade(
        manifest=manifest,
        v2=port,
        hook_v2=port,
        workspace=str(tmp_path),
    )
    active = {"context": _context(tmp_path)}
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(
        "memoryguard.mcp_server._v2_runtime_facade_factory",
        lambda workspace: facade,
    )
    monkeypatch.setattr(
        "memoryguard.mcp_server._trusted_context_for_v2",
        lambda args, workspace: (active["context"], None),
    )
    return memory, RuleV2Store(tmp_path), active


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


def _write_memory(memory_id: str, *, body: str = "seed", key: str = "seed") -> dict[str, Any]:
    return _data(_call("memoryguard_memory_write", {
        "memory_id": memory_id,
        "body": body,
        "kind": "procedure",
        "injection_policy": "always",
        "visibility": "ready",
        "evidence_ids": [f"evidence-{key}"],
        "idempotency_key": f"write-{key}",
    }))


def _seed_receipt(store: RuleV2Store, *, receipt_id: str, definition_id: str = "feedback-rule") -> str:
    definition = store.get_definition(definition_id)
    if definition is None:
        definition = store.upsert_definition(
            build_definition("Always run tests before commit", definition_id=definition_id),
        )
    store.record_receipt({
        "receipt_id": receipt_id,
        "definition_id": definition.definition_id,
        "source_rule_id": definition.definition_id,
        "share_group_id": GROUP_ID,
        "agent_instance_id": AGENT_ID,
        "project_ref": str(store.workspace),
        "session_id": "",
        "task_hash": f"task-{receipt_id}",
        "selection_digest": "selection",
        "metadata_json": "{}",
        "created_at": "2026-08-12T00:00:00+00:00",
    })
    return receipt_id


def test_v2_nonadmin_cannot_update_or_delete_cross_agent_or_group_memory(tmp_path, monkeypatch):
    memory, _rules, active = _install_v2(tmp_path, monkeypatch)

    active["context"] = _context(tmp_path, agent="b")
    _write_memory("other-agent", body="owner b", key="other-agent")
    active["context"] = _context(tmp_path, agent="b", group="other-team")
    _write_memory("other-group", body="other group", key="other-group")
    with sqlite3.connect(memory.db_path) as conn:
        before = conn.execute(
            "SELECT memory_id,body,status,revision FROM atoms ORDER BY memory_id",
        ).fetchall()

    active["context"] = _context(tmp_path)
    for memory_id in ("other-agent", "other-group"):
        assert _error(_call("memoryguard_memory_update", {
            "memory_id": memory_id,
            "body": "poison",
            "idempotency_key": f"update-denied-{memory_id}",
        })) in {"memory_not_found", "v2_memory_read_invalid"}
        assert _error(_call("memoryguard_memory_delete", {
            "memory_id": memory_id,
            "idempotency_key": f"delete-denied-{memory_id}",
        }) ) in {"memory_not_found", "v2_governance_rejected"}

    with sqlite3.connect(memory.db_path) as conn:
        assert conn.execute(
            "SELECT memory_id,body,status,revision FROM atoms ORDER BY memory_id",
        ).fetchall() == before


def test_v2_self_rule_and_owned_relevant_memory_are_mutable(tmp_path, monkeypatch):
    memory, _rules, active = _install_v2(tmp_path, monkeypatch)
    first = _write_memory("self", body="owned rule", key="self")
    relevant = _data(_call("memoryguard_memory_write", {
        "memory_id": "relevant",
        "body": "owned relevant memory",
        "kind": "fact",
        "injection_policy": "relevant",
        "visibility": "ready",
        "evidence_ids": ["evidence-relevant"],
        "idempotency_key": "write-relevant",
    }))
    assert first["atom"]["agent_instance_id"] == AGENT_ID
    assert relevant["atom"]["injection_policy"] == "relevant"

    updated = _data(_call("memoryguard_memory_update", {
        "memory_id": "self",
        "body": "owned update",
        "priority": 7,
        "idempotency_key": "update-self",
    }))
    assert updated["atom"]["body"] == "owned update"
    deleted = _data(_call("memoryguard_memory_delete", {
        "memory_id": "relevant",
        "idempotency_key": "delete-relevant",
    }))
    assert deleted["atom"]["status"] == "deleted"
    with sqlite3.connect(memory.db_path) as conn:
        assert conn.execute(
            "SELECT body,status FROM atoms WHERE memory_id='self'",
        ).fetchone() == ("owned update", "active")


def test_v2_invalid_group_audience_fails_before_rule_write(tmp_path, monkeypatch):
    _memory, rules, _active = _install_v2(tmp_path, monkeypatch)
    with sqlite3.connect(rules.db_path) as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM rule_definitions",
        ).fetchone()[0]
    result = _call("memoryguard_rule_create_auto", {
        "text": "unauthorized group rule",
        "scope": {"target_type": "group", "target_id": GROUP_ID},
        "idempotency_key": "invalid-group-audience",
    })
    assert _error(result) == "automatic_scope_expansion_denied"
    with sqlite3.connect(rules.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_definitions").fetchone()[0] == before


def test_v2_agent_audience_cannot_smuggle_foreign_project_scope(tmp_path, monkeypatch):
    _memory, rules, _active = _install_v2(tmp_path, monkeypatch)
    foreign_project = str(tmp_path / "foreign-project")
    result = _call("memoryguard_rule_create_auto", {
        "text": "invalid agent project rule",
        "scope": {
            "target_type": "agent_project",
            "target_id": AGENT_ID,
            "project_ref": foreign_project,
        },
        "idempotency_key": "invalid-foreign-project",
    })
    assert _error(result) == "other_project_scope_denied"
    with sqlite3.connect(rules.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_definitions").fetchone()[0] == 0


def test_v2_self_agent_project_rule_create_and_compensating_delete(tmp_path, monkeypatch):
    _memory, rules, _active = _install_v2(tmp_path, monkeypatch)
    created = _data(_call("memoryguard_rule_create_auto", {
        "text": "self project rule",
        "scope": {
            "target_type": "agent_project",
            "target_id": AGENT_ID,
            "project_ref": str(tmp_path),
        },
        "idempotency_key": "create-self-project",
    }))
    binding = next(
        item for item in rules.list_bindings(definition_id=created["definition_id"])
        if item.binding_id == created["binding_id"]
    )
    assert binding is not None
    assert binding.target_type == "agent_project"
    assert binding.project_ref == canonical_project_ref(str(tmp_path))

    undone = _data(_call("memoryguard_rule_undo", {
        "undo_id": created["undo_id"],
        "idempotency_key": "undo-self-project",
    }))
    assert undone["compensation"]["binding_status"] == "inactive"
    binding_after = next(
        item for item in rules.list_bindings(definition_id=created["definition_id"])
        if item.binding_id == created["binding_id"]
    )
    assert binding_after.status == "inactive"


def test_v2_rule_feedback_uses_trusted_actor_and_validates_outcome(tmp_path, monkeypatch):
    _memory, rules, _active = _install_v2(tmp_path, monkeypatch)
    receipt_id = _seed_receipt(rules, receipt_id="receipt-validity")
    invalid = _call("memoryguard_rule_feedback", {
        "receipt_id": receipt_id,
        "outcome": "invalid",
        "actor": "forged-client-actor",
        "idempotency_key": "feedback-invalid",
    })
    assert _error(invalid) == "invalid_rule_feedback_outcome"

    accepted = _data(_call("memoryguard_rule_feedback", {
        "receipt_id": receipt_id,
        "outcome": "followed",
        "actor": "",
        "idempotency_key": "feedback-valid",
    }))
    assert accepted["outcome"] == "followed"
    with sqlite3.connect(rules.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM rule_feedback_refs WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()[0] == 1


def test_v2_rule_feedback_is_idempotent_and_body_free(tmp_path, monkeypatch):
    _memory, rules, _active = _install_v2(tmp_path, monkeypatch)
    receipt_id = _seed_receipt(rules, receipt_id="receipt-idempotent")
    args = {
        "receipt_id": receipt_id,
        "outcome": "followed",
        "confidence": 0.95,
        "evidence": "private evidence body must not persist",
        "idempotency_key": "feedback-retry",
    }
    first = _data(_call("memoryguard_rule_feedback", args))
    second = _data(_call("memoryguard_rule_feedback", args))
    assert first["feedback_id"]
    with sqlite3.connect(rules.db_path) as conn:
        rows = conn.execute(
            "SELECT feedback_id,evidence_digest,metadata_json FROM rule_feedback_refs WHERE receipt_id=?",
            (receipt_id,),
        ).fetchall()
        dump = "\n".join(str(value) for row in rows for value in row)
    assert len(rows) == 1
    assert "private evidence body must not persist" not in dump
    assert second["status"] == "replayed"


def test_v2_feedback_without_host_session_does_not_narrow_binding(tmp_path, monkeypatch):
    _memory, rules, active = _install_v2(tmp_path, monkeypatch)
    active["context"] = _context(
        tmp_path,
        session_source="",
        session_trusted=False,
    )
    definition = rules.upsert_definition(
        build_definition("rule requiring explicit session evidence", definition_id="no-session-rule"),
    )
    rules.upsert_binding({
        "binding_id": "no-session-binding",
        "definition_id": definition.definition_id,
        "share_group_id": GROUP_ID,
        "target_type": "agent",
        "target_id": AGENT_ID,
        "project_ref": str(tmp_path),
        "owner_agent_id": AGENT_ID,
        "created_by": "fixture",
        "authorization": "fixture",
        "status": "active",
    })
    for index in range(3):
        receipt_id = _seed_receipt(
            rules,
            receipt_id=f"no-session-{index}",
            definition_id=definition.definition_id,
        )
        result = _data(_call("memoryguard_rule_feedback", {
            "receipt_id": receipt_id,
            "outcome": "not_applicable",
            "evidence": "session absent",
            "confidence": 1.0,
            "idempotency_key": f"no-session-feedback-{index}",
        }))
        assert result["outcome"] == "not_applicable"

    assert rules.get_definition(definition.definition_id).status == "active"
    binding = next(
        item for item in rules.list_bindings(definition_id=definition.definition_id)
        if item.binding_id == "no-session-binding"
    )
    assert binding.status == "active"
    with sqlite3.connect(rules.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM rule_feedback_refs WHERE definition_id=?",
            (definition.definition_id,),
        ).fetchone()[0] == 3
