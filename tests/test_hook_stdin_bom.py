# -*- coding: utf-8 -*-
"""Regression: Cursor may send UTF-8 BOM on hook stdin; Codex usually does not."""
from __future__ import annotations

import json
import sys
from io import BytesIO

import pytest

from memoryguard.host_hooks import read_hook_stdin_json


PAYLOAD = {"tool_name": "Shell", "tool_input": {"command": "echo test"}}
PLAIN = json.dumps(PAYLOAD, separators=(",", ":")).encode("utf-8")
BOMED = b"\xef\xbb\xbf" + PLAIN


@pytest.mark.parametrize("raw", [PLAIN, BOMED], ids=["no_bom", "with_bom"])
def test_read_hook_stdin_json_accepts_bom_and_plain(raw, monkeypatch):
    bio = BytesIO(raw)

    class _Stdin:
        buffer = bio

    monkeypatch.setattr(sys, "stdin", _Stdin())
    data = read_hook_stdin_json()
    assert data["tool_name"] == "Shell"
    assert data["tool_input"]["command"] == "echo test"


def test_read_hook_stdin_json_empty(monkeypatch):
    class _Stdin:
        buffer = BytesIO(b"")

    monkeypatch.setattr(sys, "stdin", _Stdin())
    assert read_hook_stdin_json() == {}


def test_read_hook_stdin_json_recovers_truncated_workspace_roots(monkeypatch):
    payload = {
        "conversation_id": "bf0cb918-6182-4fb8-976e-3c9f979d531e",
        "tool_name": "CallDynamicTool",
        "tool_input": {
            "namespace": "user-memoryguard",
            "toolName": "memoryguard_context_bootstrap",
            "arguments": {"task": "fix hook"},
        },
        "hook_event_name": "preToolUse",
    }
    broken = (
        json.dumps(payload, ensure_ascii=False)[:-1]
        + ',"workspace_roots":["/L:/工作\ufffd?]}'
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(broken)

    class _Stdin:
        buffer = BytesIO(b"\xef\xbb\xbf" + broken.encode("utf-8"))

    monkeypatch.setattr(sys, "stdin", _Stdin())
    data = read_hook_stdin_json()
    assert data["tool_name"] == "CallDynamicTool"
    assert data["tool_input"]["namespace"] == "user-memoryguard"
    assert data["tool_input"]["toolName"] == "memoryguard_context_bootstrap"
    assert data["conversation_id"] == "bf0cb918-6182-4fb8-976e-3c9f979d531e"


def test_read_hook_stdin_json_salvages_truncated_bootstrap_tool_output(monkeypatch):
    prefix = json.dumps(
        {
            "conversation_id": "salvage-bootstrap-1",
            "session_id": "salvage-bootstrap-1",
            "tool_name": "MCP:memoryguard_context_bootstrap",
            "tool_input": {"task": "fix hook"},
            "hook_event_name": "postToolUse",
        },
        ensure_ascii=False,
    )[:-1]
    broken = (
        prefix
        + ',"tool_output":"{\\"ok\\":true,\\"status\\":\\"ok\\",\\"data\\":{'
        + '\\"ready\\":true,\\"body\\":\\"用户说\\"stop ponytail\\"时\\"}}'
        + ',"workspace_roots":["/L:/工作\ufffd?]}'
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(broken)

    class _Stdin:
        buffer = BytesIO(b"\xef\xbb\xbf" + broken.encode("utf-8"))

    monkeypatch.setattr(sys, "stdin", _Stdin())
    data = read_hook_stdin_json()
    assert data["tool_name"] == "MCP:memoryguard_context_bootstrap"
    assert data["conversation_id"] == "salvage-bootstrap-1"
    assert data["tool_output"]["ok"] is True
    assert data["tool_output"]["ready"] is True
