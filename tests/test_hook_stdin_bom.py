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
