from __future__ import annotations

import base64
import json
import os
import sqlite3
from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.evidence.store import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.gui import GovernanceApi, SafeBridgeApi
from memoryguard.memory.store import MemoryAtomStore
from memoryguard.mcp_server import handle_request
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.content.store import ContentStore
from memoryguard.codegraph_v2.store import CodeGraphStore
from memoryguard.assets_v2.store import AssetStore
from memoryguard.projection_v2.store import ProjectionStore
from memoryguard.runtime_v2.working_memory import RuntimeStore
from memoryguard.skills_v2.store import SkillStore
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState


def _activate_v2_workspace(root: Path) -> None:
    """Build the real V2 domains and activate their persisted manifest."""
    layout = WorkspaceV2Layout(root)
    initialize_all(layout)
    MemoryAtomStore(root)
    EvidenceStore(root)
    RuleV2Store(root)
    ProjectionStore(root)
    ContentStore(root)
    RuntimeStore(root)
    CodeGraphStore(root)
    AssetStore(root)
    SkillStore(root)
    GovernanceV2(
        root,
        memory_store=MemoryAtomStore(root),
        evidence_store=EvidenceStore(root),
    )
    manager = ManifestManager(root)
    manager.transition(ManifestState.V2_BUILDING, migration_id="gui-security-fixture")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="gui-security-source",
        target_digest="gui-security-target",
        manifest_digest="gui-security-manifest",
        digests={"validator_passed": True, "checkpoints": {"gui": True}},
    )
    active = manager.transition(ManifestState.V2_ACTIVE)
    assert active.state is ManifestState.V2_ACTIVE


def _gui_api(
    root: Path,
    *,
    admin: bool,
    agent: str = "agent-a",
    group: str = "gui-security-group",
) -> GovernanceApi:
    """Return production GUI bridge backed by a process-issued V2 context."""
    _activate_v2_workspace(root)
    GroupControlService(root, write=True).bind_agent(agent, group)
    access = AccessContext(
        trusted_agent_id=agent,
        is_admin=admin,
        strict_binding=True,
        allow_anon=False,
        session_id=f"gui-security-{agent}",
        session_source="transport",
        session_trusted=True,
    )
    return GovernanceApi(
        str(root),
        _trusted_access_context=access,
    )


def _mcp_call(name: str, arguments: dict) -> dict:
    response = handle_request({
        "jsonrpc": "2.0",
        "id": name,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert response is not None
    assert response["id"] == name
    return response["result"]


def _packet(result: dict) -> dict:
    assert result.get("isError") is not True, result
    return json.loads(result["content"][0]["text"])


def _candidate(store: RuleV2Store, suffix: str, *, cooldown_until: str = "") -> dict:
    left = build_definition(
        f"must run tests before commit {suffix}",
        definition_id=f"{suffix}-left",
    )
    right = build_definition(
        f"must run tests before commit {suffix}",
        definition_id=f"{suffix}-right",
    )
    left = store.upsert_definition(left)
    right = store.upsert_definition(right)
    proposal_id = f"{suffix}-proposal"
    store.record_merge_proposal({
        "proposal_id": proposal_id,
        "definition_ids_json": json.dumps(
            [left.definition_id, right.definition_id],
            ensure_ascii=False,
        ),
        "status": "candidate",
        "metadata_json": json.dumps({
            "definition_revision_a": left.revision,
            "definition_revision_b": right.revision,
            "cooldown_until": cooldown_until,
        }, ensure_ascii=False, sort_keys=True),
    })
    return {
        "proposal_id": proposal_id,
        "expected_definition_revisions": {
            left.definition_id: left.revision,
            right.definition_id: right.revision,
        },
    }


def _secret(seed: str) -> str:
    raw = (seed.encode("utf-8") * 32)[:32]
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _issue_capability(tmp_path: Path, proposal_id: str, suffix: str) -> str:
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


def _error_packet(result: dict) -> dict:
    assert result.get("isError") is True, result
    return json.loads(result["content"][0]["text"])


def test_safe_bridge_never_injects_admin_override(monkeypatch):
    calls = {}

    class FakeApi:
        def lock_memory(self, confirmed=False, **kwargs):
            calls["confirmed"] = confirmed
            calls["kwargs"] = kwargs
            return {"ok": True}

        def submit_request(self, method, args):
            raise AssertionError(f"unexpected deferred request: {method} {args}")

    monkeypatch.setenv("MEMORYGUARD_SANDBOX", "0")
    bridge = SafeBridgeApi("unused", direct_mutations=True)
    bridge._inner = FakeApi()

    assert bridge.request_mutation("lock_memory") == {"ok": True}
    assert calls == {"confirmed": True, "kwargs": {}}


def test_gui_cannot_forge_admin_or_rewrite_security_environment(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("MEMORYGUARD_ADMIN", raising=False)
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "1")
    monkeypatch.setenv("MEMORYGUARD_SANDBOX", "1")

    result = _gui_api(tmp_path, admin=False).lock_memory(
        "missing", "missing-group", _admin_override=True,
    )
    assert result["ok"] is False
    assert result["code"] == "admin_capability_required"
    assert result["error"] == "admin_capability_required"

    from memoryguard import gui

    class FakeServer:
        def __init__(self, address, handler):
            self.address = address
            self.handler = handler

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(gui, "_find_free_port", lambda: 43123)
    monkeypatch.setattr(gui.http.server, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(gui, "render_interactive_html", lambda: "<html></html>")
    monkeypatch.delenv("MEMORYGUARD_ADMIN", raising=False)
    monkeypatch.delenv("MEMORYGUARD_ALLOW_ANON", raising=False)

    code, url = gui.open_localhost_window(str(tmp_path), auto_open=False)
    assert code == 0
    assert url.endswith("43123/")
    assert "MEMORYGUARD_ADMIN" not in os.environ
    assert "MEMORYGUARD_ALLOW_ANON" not in os.environ
    assert os.environ["MEMORYGUARD_SANDBOX"] == "1"


def test_mcp_capability_governance_flow_remains_authorized(tmp_path, monkeypatch):
    _activate_v2_workspace(tmp_path)
    group = "gui-security-group"
    groups = GroupControlService(tmp_path, write=True)
    groups.bind_agent("trusted-admin", group, idempotency_key="bind-trusted-admin")
    groups.bind_agent("attacker", group, idempotency_key="bind-attacker")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "trusted-admin")
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_SESSION_ID", "gui-security-session")
    monkeypatch.setenv("MEMORYGUARD_SESSION_SOURCE", "transport")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", str(tmp_path))
    store = RuleV2Store(tmp_path)

    approval = _candidate(store, "approval")
    issue_arguments = {
        "workspace": str(tmp_path),
        "proposal_id": approval["proposal_id"],
        "idempotency_key": "issue-approval",
        "mutation_receipt": {"receipt_id": "receipt-issue-approval"},
        "recovery_secret": _secret("recovery-approval"),
    }

    # Neither a client-supplied admin bit nor a forged principal can authorize
    # a capability issue.  The connection-owned context remains authoritative.
    forged = _error_packet(_mcp_call(
        "memoryguard_rule_merge_capability_issue",
        {**issue_arguments, "agent_instance_id": "attacker", "is_admin": True},
    ))
    assert forged["code"] == "request_failed"
    assert "capability_token" not in json.dumps(forged)

    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "attacker")
    monkeypatch.delenv("MEMORYGUARD_ADMIN", raising=False)
    unauthorized = _error_packet(_mcp_call(
        "memoryguard_rule_merge_capability_issue",
        {**issue_arguments, "agent_instance_id": "attacker"},
    ))
    assert unauthorized["code"] == "native_admin_capability_required"
    assert "capability_token" not in json.dumps(unauthorized)

    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "trusted-admin")
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    token = _issue_capability(tmp_path, approval["proposal_id"], "approval")
    approve_arguments = {
        "workspace": str(tmp_path),
        "proposal_id": approval["proposal_id"],
        "capability_token": token,
        "expected_definition_revisions": approval["expected_definition_revisions"],
        "idempotency_key": "approve-approval",
        "mutation_receipt": {"receipt_id": "receipt-approve-approval"},
    }
    approved = _packet(_mcp_call(
        "memoryguard_rule_merge_approve", approve_arguments,
    ))
    assert approved["data"]["proposal_id"] == approval["proposal_id"]
    replay = _packet(_mcp_call(
        "memoryguard_rule_merge_approve", approve_arguments,
    ))
    assert replay["data"].get("idempotent_replay") is True
    assert "capability_token" not in json.dumps(replay)
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT status FROM rule_merge_proposals WHERE proposal_id=?",
            (approval["proposal_id"],),
        ).fetchone()[0] == "approved"

    acknowledged = _candidate(store, "acknowledge")
    ack_token = _issue_capability(tmp_path, acknowledged["proposal_id"], "acknowledge")
    acknowledged_result = _packet(_mcp_call(
        "memoryguard_rule_merge_acknowledge",
        {
            "workspace": str(tmp_path),
            "proposal_id": acknowledged["proposal_id"],
            "capability_token": ack_token,
            "idempotency_key": "ack-acknowledge",
            "mutation_receipt": {"receipt_id": "receipt-ack-acknowledge"},
        },
    ))
    assert acknowledged_result["data"]["metadata"]["first_merge_acknowledged"] is True

    cooldown = _candidate(
        store, "cooldown", cooldown_until="2099-01-01T00:00:00+00:00",
    )
    cooldown_token = _issue_capability(tmp_path, cooldown["proposal_id"], "cooldown")
    cleared = _packet(_mcp_call("memoryguard_rule_merge_cooldown_clear", {
        "workspace": str(tmp_path),
        "proposal_id": cooldown["proposal_id"],
        "capability_token": cooldown_token,
        "idempotency_key": "clear-cooldown",
        "mutation_receipt": {"receipt_id": "receipt-clear-cooldown"},
    }))
    assert cleared["data"]["metadata"]["cooldown_until"] == ""
