from __future__ import annotations

import json
import os

from memoryguard.gui import GovernanceApi, SafeBridgeApi
from memoryguard.mcp_server import handle_request
from memoryguard.rule_definition import build_definition
from memoryguard.rule_merge_store import RuleMergeStore


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


def _candidate(store: RuleMergeStore, suffix: str, *, cooldown_until: str = "") -> dict:
    left = build_definition(
        f"must run tests before commit {suffix}",
        definition_id=f"{suffix}-left",
    )
    right = build_definition(
        f"must run tests before commit {suffix}",
        definition_id=f"{suffix}-right",
    )
    store.upsert_definition(left)
    store.upsert_definition(right)
    return store.create_proposal(
        [left.definition_id, right.definition_id],
        1.0,
        cooldown_until=cooldown_until,
        readiness_digest="fixture-readiness",
        definition_a=left,
        definition_b=right,
    )


def _issue_capability(tmp_path, proposal_id: str) -> str:
    payload = _packet(_mcp_call(
        "memoryguard_rule_merge_capability_issue",
        {"proposal_id": proposal_id},
    ))
    token = payload["capability_token"]
    assert token and not token.startswith("admin:")
    return token


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

    result = GovernanceApi(tmp_path).lock_memory(
        "missing", "missing-group", _admin_override=True,
    )
    assert result["ok"] is False
    assert "admin capability required" in result["error"]

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
    monkeypatch.setattr(gui.http.server, "HTTPServer", FakeServer)
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
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "trusted-admin")
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    store = RuleMergeStore(tmp_path)

    approval = _candidate(store, "approval")
    token = _issue_capability(tmp_path, approval["proposal_id"])
    approved = _packet(_mcp_call("memoryguard_rule_merge_approve", {
        "proposal_id": approval["proposal_id"],
        "capability_token": token,
    }))
    assert approved["ok"] is True
    assert store.get_proposal(approval["proposal_id"])["status"] == "approved"

    acknowledged = _candidate(store, "acknowledge")
    ack_token = _issue_capability(tmp_path, acknowledged["proposal_id"])
    acknowledged_result = _packet(_mcp_call(
        "memoryguard_rule_merge_acknowledge",
        {
            "proposal_id": acknowledged["proposal_id"],
            "capability_token": ack_token,
        },
    ))
    assert acknowledged_result["ok"] is True
    assert store.get_proposal(acknowledged["proposal_id"])["first_merge_acknowledged"] is True

    cooldown = _candidate(
        store, "cooldown", cooldown_until="2099-01-01T00:00:00+00:00",
    )
    cooldown_token = _issue_capability(tmp_path, cooldown["proposal_id"])
    cleared = _packet(_mcp_call("memoryguard_rule_merge_cooldown_clear", {
        "proposal_id": cooldown["proposal_id"],
        "capability_token": cooldown_token,
    }))
    assert cleared["ok"] is True
    assert store.get_proposal(cooldown["proposal_id"])["cooldown_until"] == ""
