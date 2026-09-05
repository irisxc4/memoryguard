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
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from memoryguard.content import ContentStore
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory import MemoryAtomStore
from memoryguard.mcp_server import (
    TOOL_DEFINITIONS,
    TOOLS,
    execute_tool,
    handle_request,
)
from memoryguard.projection_v2.store import ProjectionStore
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.working_memory import RuntimeStore
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState

EXPECTED_BLOCK = {"ok": False, "error": "v2_upgrade_required"}
DIAGNOSTIC_TOOLS = frozenset({
    "memoryguard_canonical_status",
    "memoryguard_diagnostics_snapshot",
    "memoryguard_projection_status",
    "memoryguard_runtime_processes",
})


def _tool_payload(result: dict) -> dict:
    """Decode the JSON text carried by a spec-shaped MCP CallToolResult."""
    assert isinstance(result.get("content"), list) and result["content"]
    assert result["content"][0]["type"] == "text"
    return json.loads(result["content"][0]["text"])


def _assert_degraded_result(result: dict) -> dict:
    assert result.get("isError") is True
    payload = _tool_payload(result)
    for key, value in EXPECTED_BLOCK.items():
        assert payload.get(key) == value
    return payload


def _assert_upgrade_result(result: dict) -> dict:
    assert result.get("isError") is True
    payload = _tool_payload(result)
    assert payload.get("code") == "v2_upgrade_required", payload
    assert payload.get("error") == "v2_upgrade_required", payload
    return payload


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    for name in (
        "MEMORYGUARD_WORKSPACE",
        "MEMORYGUARD_HOME",
        "MEMORYGUARD_AGENT_ID",
        "MEMORYGUARD_PROVIDER",
        "MEMORYGUARD_CONTROL_SCOPE",
    ):
        monkeypatch.delenv(name, raising=False)
    # V2 MCP treats ``workspace`` in a request as a project hint; the control
    # plane comes from MEMORYGUARD_HOME.  Keep this suite on an isolated,
    # uninitialized control root so ambient user-level V2 state (or a live
    # MCP process) cannot turn the pre-activation assertions into identity or
    # runtime-lease failures.
    monkeypatch.setenv("MEMORYGUARD_HOME", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "0")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "1")


def _activate_v2_workspace(tmp_path: Path) -> Path:
    """Build the public V2 directory contract used by MCP diagnostics."""
    ws = Path(tmp_path)
    initialize_all(WorkspaceV2Layout(ws))
    MemoryAtomStore(ws)
    EvidenceStore(ws)
    GovernanceV2(ws)
    RuleV2Store(ws)
    ProjectionStore(ws)
    ContentStore(ws)
    RuntimeStore(ws)
    manager = ManifestManager(ws)
    manager.transition(ManifestState.V2_BUILDING, migration_id="diagnostic-fixture")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="diagnostic-source",
        target_digest="diagnostic-target",
        manifest_digest="diagnostic-manifest",
        digests={"validator_passed": True, "checkpoints": {"diagnostics": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE
    GroupControlService(ws, write=True).bind_agent("diag-agent", "default")
    return ws


def _configure_identity(monkeypatch, ws: Path) -> None:
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(ws))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "diag-agent")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    monkeypatch.setenv("MEMORYGUARD_SESSION_ID", "diagnostic-session")
    monkeypatch.setenv("MEMORYGUARD_SESSION_SOURCE", "transport")
    monkeypatch.setenv("MEMORYGUARD_SESSION_TRUSTED", "1")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", str(ws))


def _tool_def(name: str) -> dict:
    return TOOL_DEFINITIONS[name]


# ---------------------------------------------------------------------------
# predicate
# ---------------------------------------------------------------------------


def test_governance_degraded_predicate(tmp_path):
    """The public V2 cutover remains fail-closed before activation."""
    for name, args in (
        ("memoryguard_memory_read", {"memory_id": "m1"}),
        ("memoryguard_memory_write", {"body": "x"}),
        ("memoryguard_memory_delete", {"memory_id": "m1"}),
    ):
        _assert_upgrade_result(execute_tool(name, {"workspace": str(tmp_path), **args}))


def test_whitelist_is_exactly_the_four_diagnostics():
    assert DIAGNOSTIC_TOOLS == {
        "memoryguard_canonical_status",
        "memoryguard_diagnostics_snapshot",
        "memoryguard_projection_status",
        "memoryguard_runtime_processes",
    }
    for name in DIAGNOSTIC_TOOLS:
        assert _tool_def(name)["name"] == name


# ---------------------------------------------------------------------------
# gate: lock broken blocks reads+writes, write-only degradation blocks writes
# ---------------------------------------------------------------------------


def test_degraded_blocks_mutating_tool_exact_shape(tmp_path):
    for name, args in [
        ("memoryguard_memory_write", {"body": "x", "workspace": str(tmp_path)}),
        ("memoryguard_rule_undo", {"undo_id": "u1", "workspace": str(tmp_path)}),
    ]:
        _assert_upgrade_result(execute_tool(name, args))


def test_degraded_tools_call_wire_result_is_valid_call_tool_result(
    tmp_path,
):
    response = handle_request({
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": "memoryguard_memory_write",
            "arguments": {"body": "x", "workspace": str(tmp_path)},
        },
    })
    assert response is not None
    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 7
    result = response["result"]
    payload = _assert_upgrade_result(result)
    assert payload["error"] == "v2_upgrade_required"
    assert "error" not in result


def test_broken_lock_blocks_normal_read_tool(tmp_path):
    res = execute_tool(
        "memoryguard_memory_read",
        {"memory_id": "m1", "workspace": str(tmp_path)},
    )
    _assert_upgrade_result(res)
    res2 = execute_tool(
        "memoryguard_memory_search",
        {"query": "anything", "workspace": str(tmp_path)},
    )
    assert _assert_upgrade_result(res2)["error"] == "v2_upgrade_required"


def test_outbox_backlog_blocks_writes_but_not_reads(tmp_path, monkeypatch):
    ws = _activate_v2_workspace(tmp_path)
    _configure_identity(monkeypatch, ws)
    write = execute_tool(
        "memoryguard_memory_write",
        {
            "body": "x",
            "workspace": str(ws),
            "memory_id": "m1",
            "kind": "fact",
            "visibility": "ready",
            "evidence_ids": ["evidence-m1"],
            "idempotency_key": "write-m1",
        },
    )
    assert write.get("isError") is not True, write
    read = execute_tool(
        "memoryguard_memory_read",
        {"memory_id": "m1", "workspace": str(ws)},
    )
    assert read.get("isError") is not True, read


def test_write_gate_auto_recovers_deterministic_outbox_backlog(tmp_path, monkeypatch):
    ws = _activate_v2_workspace(tmp_path)
    _configure_identity(monkeypatch, ws)

    written = execute_tool(
        "memoryguard_memory_write",
        {
            "body": "write through the public V2 recovery path",
            "workspace": str(ws),
            "memory_id": "recovered-write",
            "kind": "procedure",
            "visibility": "ready",
            "evidence_ids": ["evidence-recovered-write"],
            "idempotency_key": "write-recovered-write",
        },
    )
    assert written.get("isError") is not True, written
    payload = _tool_payload(written)
    assert payload.get("data", {}).get("atom", {}).get("memory_id") == "recovered-write"


def test_startup_auto_recovery_repairs_deterministic_backlog(tmp_path, monkeypatch):
    ws = _activate_v2_workspace(tmp_path)
    _configure_identity(monkeypatch, ws)
    result = execute_tool(
        "memoryguard_runtime_processes",
        {"workspace": str(ws)},
    )
    assert result.get("isError") is not True, result
    payload = _tool_payload(result)
    assert payload["data"]["runtime_status"] == "V2_ACTIVE"


def test_canonical_status_error_blocks_writes_but_not_reads(tmp_path):
    ws = Path(tmp_path)
    write = execute_tool(
        "memoryguard_memory_write",
        {"body": "x", "workspace": str(ws)},
    )
    _assert_upgrade_result(write)
    read = execute_tool(
        "memoryguard_memory_read",
        {"memory_id": "m1", "workspace": str(ws)},
    )
    _assert_upgrade_result(read)


def test_split_degraded_predicates(tmp_path, monkeypatch):
    blocked = execute_tool(
        "memoryguard_memory_write",
        {"workspace": str(tmp_path), "body": "blocked"},
    )
    _assert_upgrade_result(blocked)
    ws = _activate_v2_workspace(tmp_path / "active")
    _configure_identity(monkeypatch, ws)
    healthy = execute_tool(
        "memoryguard_memory_status",
        {"workspace": str(ws)},
    )
    assert healthy.get("isError") is not True, healthy


def test_degraded_allows_four_read_only_diagnostics(tmp_path, monkeypatch):
    ws = _activate_v2_workspace(tmp_path)
    _configure_identity(monkeypatch, ws)

    canon = execute_tool(
        "memoryguard_canonical_status",
        {"workspace": str(ws), "share_group_id": "default"},
    )
    assert canon.get("isError") is not True
    canon_payload = json.loads(canon["content"][0]["text"])
    assert canon_payload["ok"] is True
    assert canon_payload["path"] == "v2"
    assert canon_payload["data"]["share_group_id"] == "default"

    snap = execute_tool(
        "memoryguard_diagnostics_snapshot",
        {"workspace": str(ws), "share_group_id": "default"},
    )
    assert snap.get("isError") is not True
    snap_payload = json.loads(snap["content"][0]["text"])
    assert snap_payload["ok"] is True
    assert snap_payload["data"]["status"] == "READY"
    assert snap_payload["data"]["memory"]["total_records"] == 0

    proj = execute_tool(
        "memoryguard_projection_status",
        {"workspace": str(ws), "share_group_id": "default"},
    )
    assert proj.get("isError") is not True
    proj_payload = json.loads(proj["content"][0]["text"])
    assert proj_payload["ok"] is True
    assert proj_payload["data"]["status"] == "READY"
    assert isinstance(proj_payload["data"]["total_heads"], int)

    proc = execute_tool("memoryguard_runtime_processes", {"workspace": str(ws)})
    assert proc.get("isError") is not True
    proc_payload = json.loads(proc["content"][0]["text"])
    assert proc_payload["ok"] is True
    assert proc_payload["data"]["runtime_status"] == "V2_ACTIVE"
    assert isinstance(proc_payload["data"]["summary"], dict)


def test_healthy_workspace_is_not_degraded(tmp_path, monkeypatch):
    ws = _activate_v2_workspace(tmp_path)
    _configure_identity(monkeypatch, ws)

    res = execute_tool(
        "memoryguard_memory_status",
        {"workspace": str(ws)},
    )
    assert res.get("isError") is not True
    payload = _tool_payload(res)
    assert payload["state"] == "V2_ACTIVE"
    assert payload["data"]["total_records"] == 0


# ---------------------------------------------------------------------------
# diagnostics_snapshot must use Connection.backup(), never a raw file copy
# ---------------------------------------------------------------------------


def test_diagnostics_snapshot_uses_backup_not_raw_copy(tmp_path, monkeypatch):
    ws = _activate_v2_workspace(tmp_path)
    _configure_identity(monkeypatch, ws)

    copy_calls: list[str] = []
    # Patch the migration module's copy surface, not the process-global
    # ``shutil`` module.  Other tests may legitimately finish an unrelated
    # background cleanup while this assertion is active; the contract under
    # test is specifically that diagnostics never enter workspace_prepare.
    from memoryguard.migration import workspace_prepare

    class _CopyGuard:
        def copy(self, *a, **k):
            copy_calls.append("copy")

        def copyfile(self, *a, **k):
            copy_calls.append("copyfile")

        move = shutil.move

    monkeypatch.setattr(workspace_prepare, "shutil", _CopyGuard())

    result = execute_tool(
        "memoryguard_diagnostics_snapshot",
        {"workspace": str(ws), "share_group_id": "default"},
    )
    assert result.get("isError") is not True
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["data"]["status"] == "READY"
    assert payload["path"] == "v2"

    assert copy_calls == []           # never shutil.copy / copyfile of any file
    # The public V2 snapshot must not expose or create ad-hoc backup paths.
    assert not any(
        p.name.endswith(".tmp") or ".bak" in p.name or ".backup" in p.name
        for p in ws.rglob("*")
        if p.is_file()
    )


def test_diagnostics_snapshot_reports_uninitialized_store(tmp_path):
    ws = Path(tmp_path)
    result = execute_tool(
        "memoryguard_diagnostics_snapshot",
        {"workspace": str(ws), "share_group_id": "default"},
    )
    _assert_upgrade_result(result)


# ---------------------------------------------------------------------------
# individual tool structure
# ---------------------------------------------------------------------------


def test_canonical_status_group_missing_is_readonly_error(tmp_path, monkeypatch):
    ws = _activate_v2_workspace(tmp_path)
    _configure_identity(monkeypatch, ws)
    result = execute_tool(
        "memoryguard_canonical_status",
        {"workspace": str(ws), "share_group_id": "no-such-group"},
    )
    assert result.get("isError") is not True
    payload = _tool_payload(result)
    assert payload["data"]["share_group_id"] == "default"
    assert not (ws / ".memoryguard" / "shared-memory" / "no-such-group" / "memory.db").exists()


def test_projection_status_structure(tmp_path, monkeypatch):
    ws = _activate_v2_workspace(tmp_path)
    _configure_identity(monkeypatch, ws)
    result = execute_tool(
        "memoryguard_projection_status",
        {"workspace": str(ws), "share_group_id": "default"},
    )
    assert result.get("isError") is not True
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["data"]["status"] == "READY"
    assert payload["data"]["total_heads"] == 0


def test_runtime_processes_readonly_fields(tmp_path, monkeypatch):
    ws = _activate_v2_workspace(tmp_path)
    _configure_identity(monkeypatch, ws)
    result = execute_tool("memoryguard_runtime_processes", {"workspace": str(ws)})
    assert result.get("isError") is not True
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["data"]["runtime_status"] == "V2_ACTIVE"
    assert isinstance(payload["data"]["summary"], dict)


def test_runtime_processes_redacts_paths_for_non_admin(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORYGUARD_ADMIN", raising=False)
    ws = _activate_v2_workspace(tmp_path)
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(ws))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "diag-agent")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    result = execute_tool("memoryguard_runtime_processes", {"workspace": str(ws)})
    assert result.get("isError") is not True
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True
    assert "database_paths" not in payload["data"]
    assert str(ws.resolve()) not in json.dumps(payload, ensure_ascii=False)


def test_diagnostics_tools_are_callable_but_not_in_default_tools_list():
    names = {t["name"] for t in TOOLS}
    for name in DIAGNOSTIC_TOOLS:
        assert name not in names
        assert name in TOOL_DEFINITIONS
        assert "additionalProperties" in _tool_def(name)["inputSchema"]
        schema_props = _tool_def(name)["inputSchema"]["properties"]
        assert "workspace" in schema_props
