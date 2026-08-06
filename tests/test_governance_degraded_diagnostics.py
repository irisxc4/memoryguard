"""Req9: governance-degraded read-only diagnostics.

When governance is degraded the MCP layer must block every normal tool and
every mutation, and allow only the four read-only diagnostics:

    memoryguard_canonical_status
    memoryguard_diagnostics_snapshot
    memoryguard_projection_status
    memoryguard_runtime_processes

The SQLite snapshot must go through ``sqlite3.Connection.backup()`` -- never a
raw copy of ``memory.db`` or any WAL file -- and accept no arbitrary SQL or
file paths.
"""
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from memoryguard import mcp_server
from memoryguard.mcp_server import (
    TOOLS,
    _DEGRADED_WHITELIST,
    _governance_diagnostics_state,
    execute_tool,
    governance_degraded,
)

EXPECTED_BLOCK = {"ok": False, "error": "governance_degraded", "degraded": True}


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    monkeypatch.delenv("MEMORYGUARD_WORKSPACE", raising=False)
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "0")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "1")


def _degraded_state(*_args, **_kwargs):
    """A state that must trip the degraded gate."""
    return {
        "lock": {"acquirable": False, "error": "GovernanceLockTimeout: probe"},
        "canonical": None,
    }


def _make_workspace(tmp_path: Path) -> Path:
    """Initialize the rule-intelligence store and the default shared group."""
    ws = Path(tmp_path)
    from memoryguard.rule_merge_store import RuleMergeStore
    from memoryguard.shared_memory_store import SharedMemoryStore

    RuleMergeStore(ws)
    SharedMemoryStore(ws, "default")
    return ws


def _tool_def(name: str) -> dict:
    return next(t for t in TOOLS if t["name"] == name)


# ---------------------------------------------------------------------------
# predicate
# ---------------------------------------------------------------------------


def test_governance_degraded_predicate():
    assert governance_degraded({"lock": {"acquirable": False, "error": "x"}, "canonical": None}) is True
    assert governance_degraded({"lock": {"acquirable": True}, "canonical": {"outbox_pending": 3}}) is True
    assert governance_degraded({"lock": {"acquirable": True}, "canonical": {"projection_error": "boom"}}) is True
    # healthy / unknown states are never degraded (conservative)
    assert governance_degraded({"lock": {"acquirable": True}, "canonical": {"outbox_pending": 0}}) is False
    assert governance_degraded({"lock": {"acquirable": True}, "canonical": None}) is False
    assert governance_degraded({"lock": {"acquirable": None, "error": ""}, "canonical": None}) is False
    assert governance_degraded({}) is False
    assert governance_degraded("not-a-dict") is False
    # not-canonical-ready alone (pending model job / graph not built) is NOT degraded
    assert governance_degraded({
        "lock": {"acquirable": True},
        "canonical": {"canonical_ready": False, "outbox_pending": 0,
                      "projection_error": "", "reconciliation_in_flight": 1},
    }) is False


def test_whitelist_is_exactly_the_four_diagnostics():
    assert _DEGRADED_WHITELIST == {
        "memoryguard_canonical_status",
        "memoryguard_diagnostics_snapshot",
        "memoryguard_projection_status",
        "memoryguard_runtime_processes",
    }
    for name in _DEGRADED_WHITELIST:
        assert _tool_def(name)["name"] == name


# ---------------------------------------------------------------------------
# gate: mutations + normal tools blocked, whitelist allowed
# ---------------------------------------------------------------------------


def test_degraded_blocks_mutating_tool_exact_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "_governance_diagnostics_state", _degraded_state)
    for name, args in [
        ("memoryguard_memory_write", {"body": "x", "workspace": str(tmp_path)}),
        ("memoryguard_rule_undo", {"undo_id": "u1", "workspace": str(tmp_path)}),
    ]:
        assert execute_tool(name, args) == EXPECTED_BLOCK, name


def test_degraded_blocks_normal_read_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "_governance_diagnostics_state", _degraded_state)
    res = execute_tool(
        "memoryguard_memory_read",
        {"memory_id": "m1", "workspace": str(tmp_path)},
    )
    assert res == EXPECTED_BLOCK
    res2 = execute_tool(
        "memoryguard_memory_search",
        {"query": "anything", "workspace": str(tmp_path)},
    )
    assert res2.get("error") == "governance_degraded"


def test_degraded_allows_four_read_only_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "_governance_diagnostics_state", _degraded_state)
    ws = _make_workspace(tmp_path)

    canon = execute_tool(
        "memoryguard_canonical_status",
        {"workspace": str(ws), "share_group_id": "default"},
    )
    assert canon.get("isError") is not True
    canon_payload = json.loads(canon["content"][0]["text"])
    assert canon_payload["ok"] is True
    assert "canonical_ready" in canon_payload
    assert "failures" in canon_payload and isinstance(canon_payload["failures"], list)
    assert "checks" in canon_payload and isinstance(canon_payload["checks"], dict)
    assert "read_path" in canon_payload

    snap = execute_tool(
        "memoryguard_diagnostics_snapshot",
        {"workspace": str(ws), "share_group_id": "default"},
    )
    assert snap.get("isError") is not True
    snap_payload = json.loads(snap["content"][0]["text"])
    assert snap_payload["ok"] is True
    assert snap_payload["initialized"] is True
    assert isinstance(snap_payload["jobs_by_status"], dict)
    assert isinstance(snap_payload["canonical_activation"], list)
    assert isinstance(snap_payload["source_links"], int)
    assert isinstance(snap_payload["bindings"], int)

    proj = execute_tool(
        "memoryguard_projection_status",
        {"workspace": str(ws), "share_group_id": "default"},
    )
    assert proj.get("isError") is not True
    proj_payload = json.loads(proj["content"][0]["text"])
    assert proj_payload["ok"] is True
    assert "projection_lag" in proj_payload
    assert "projection_error" in proj_payload
    assert "scopes" in proj_payload and isinstance(proj_payload["scopes"], list)

    proc = execute_tool("memoryguard_runtime_processes", {"workspace": str(ws)})
    assert proc.get("isError") is not True
    proc_payload = json.loads(proc["content"][0]["text"])
    assert proc_payload["ok"] is True
    assert isinstance(proc_payload["pid"], int)
    assert proc_payload["memoryguard_version"]
    assert isinstance(proc_payload["code_fingerprint"], str)
    assert proc_payload["control_workspace"]
    assert isinstance(proc_payload["database_paths"], list)


def test_healthy_workspace_is_not_degraded(tmp_path, monkeypatch):
    ws = _make_workspace(tmp_path)
    from memoryguard.agent_binding import AgentBindingStore

    AgentBindingStore(ws).bind_agent("diag-agent", "default")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(ws))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "diag-agent")

    state = _governance_diagnostics_state(ws, "default")
    assert state["lock"]["acquirable"] is True
    # a fresh group has no canonical activation -> not ready, but that alone
    # must never trip the degraded gate
    assert state["canonical"]["canonical_ready"] is False
    assert governance_degraded(state) is False

    res = execute_tool(
        "memoryguard_memory_status",
        {"workspace": str(ws)},
    )
    assert res.get("error") != "governance_degraded"
    assert res.get("isError") is not True


# ---------------------------------------------------------------------------
# diagnostics_snapshot must use Connection.backup(), never a raw file copy
# ---------------------------------------------------------------------------


def test_diagnostics_snapshot_uses_backup_not_raw_copy(tmp_path, monkeypatch):
    ws = _make_workspace(tmp_path)
    store_dir = ws / ".memoryguard" / "rule-intelligence"

    copy_calls: list[str] = []
    monkeypatch.setattr(shutil, "copy", lambda *a, **k: copy_calls.append("copy"))
    monkeypatch.setattr(shutil, "copyfile", lambda *a, **k: copy_calls.append("copyfile"))

    # ``sqlite3.Connection`` is an immutable builtin, so the backup path is
    # captured by wrapping the source connection returned by ``store._db()``.
    from memoryguard.rule_merge_store import RuleMergeStore

    class _RecordingConn:
        def __init__(self, conn):
            self._conn = conn
            self.backup_calls = 0

        def __getattr__(self, item):
            return getattr(self._conn, item)

        def backup(self, target, *a, **k):
            self.backup_calls += 1
            return self._conn.backup(target, *a, **k)

    rec = {"backup_calls": 0}
    _orig_db = RuleMergeStore._db

    def _recording_db(self):
        conn = _orig_db(self)
        proxy = _RecordingConn(conn)
        orig = proxy.backup

        def _counted_backup(target, *a, **k):
            rec["backup_calls"] += 1
            return orig(target, *a, **k)

        proxy.backup = _counted_backup
        return proxy

    monkeypatch.setattr(RuleMergeStore, "_db", _recording_db)

    result = execute_tool(
        "memoryguard_diagnostics_snapshot",
        {"workspace": str(ws), "share_group_id": "default"},
    )
    assert result.get("isError") is not True
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["initialized"] is True
    for key in ("jobs_by_status", "canonical_activation", "projection",
                "source_links", "bindings"):
        assert key in payload, key

    assert copy_calls == []           # never shutil.copy / copyfile of any file
    assert rec["backup_calls"] > 0    # went through Connection.backup()
    # no WAL / SHM / copy artifacts produced by the snapshot (journal-mode store
    # leaves no -wal/-shm; a raw WAL copy would create or overwrite them)
    names = {p.name for p in store_dir.iterdir()}
    assert "memory.db-wal" not in names
    assert "memory.db-shm" not in names
    assert not any(
        p.name.endswith(".tmp") or ".bak" in p.name or ".backup" in p.name
        for p in store_dir.iterdir()
    )
    # still reads the real store contents through the backup() view
    assert payload["source_links"] >= 0


def test_diagnostics_snapshot_reports_uninitialized_store(tmp_path):
    ws = Path(tmp_path)
    result = execute_tool(
        "memoryguard_diagnostics_snapshot",
        {"workspace": str(ws), "share_group_id": "default"},
    )
    assert result.get("isError") is not True
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["initialized"] is False
    assert payload["reason"] == "rule_intelligence_store_not_initialized"


# ---------------------------------------------------------------------------
# individual tool structure
# ---------------------------------------------------------------------------


def test_canonical_status_group_missing_is_readonly_error(tmp_path):
    ws = _make_workspace(tmp_path)  # rule-intelligence store exists, no other group
    result = execute_tool(
        "memoryguard_canonical_status",
        {"workspace": str(ws), "share_group_id": "no-such-group"},
    )
    assert result.get("isError") is not True
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is False
    assert payload["error"] == "group_not_found"
    # read-only: the missing group must NOT have been created
    assert not (ws / ".memoryguard" / "shared-memory" / "no-such-group" / "memory.db").exists()


def test_projection_status_structure(tmp_path):
    ws = _make_workspace(tmp_path)
    result = execute_tool(
        "memoryguard_projection_status",
        {"workspace": str(ws), "share_group_id": "default"},
    )
    assert result.get("isError") is not True
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["projection_lag"] == 0
    assert payload["projection_error"] == ""
    assert isinstance(payload["scopes"], list)


def test_runtime_processes_readonly_fields(tmp_path):
    ws = _make_workspace(tmp_path)
    result = execute_tool("memoryguard_runtime_processes", {"workspace": str(ws)})
    assert result.get("isError") is not True
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["pid"] == os.getpid()
    assert payload["memoryguard_version"]
    assert payload["control_workspace"] == str(ws.resolve())
    assert any("rule-intelligence" in p for p in payload["database_paths"])


def test_diagnostics_tools_are_registered_in_tools_list():
    names = {t["name"] for t in TOOLS}
    for name in _DEGRADED_WHITELIST:
        assert name in names
        assert "additionalProperties" in _tool_def(name)["inputSchema"]
        schema_props = _tool_def(name)["inputSchema"]["properties"]
        assert "workspace" in schema_props
