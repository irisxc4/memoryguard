from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.runtime_v2.external_mcp_native import (
    NativeExternalMCPService,
    bind_external_mcp_test_capability,
)
import memoryguard.runtime_v2.external_mcp_native as external_mcp_native
from memoryguard.runtime_v2.native_ports import (
    NativePortError,
    NativeV2RuntimePort,
    bind_native_transport_context,
)


def _context(workspace: Path, *, admin: bool = False, group: str = "g", project: str = "p"):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="external-agent",
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id="external-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace),
        share_group_id=group,
        project_ref=project,
        provider="codex",
        runtime_role="root",
    )


def _write_config(workspace: Path, value: object) -> Path:
    path = workspace / ".memoryguard" / "external-mcp" / "servers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def test_missing_config_is_no_source_and_zero_write(tmp_path: Path):
    service = NativeExternalMCPService(tmp_path)
    result = service.list_external_mcp_servers(context=_context(tmp_path))
    assert result == {
        "ok": True,
        "status": "NO_SOURCE",
        "service": "external_mcp_native",
        "servers": [],
        "total": 0,
    }
    assert not (tmp_path / ".memoryguard").exists()


def test_plain_identity_and_payload_path_admin_spoof_fail_closed(tmp_path: Path):
    service = NativeExternalMCPService(tmp_path)
    forged = service.list_external_mcp_servers(
        {"admin": True, "workspace_id": str(tmp_path), "path": str(tmp_path.parent)},
        context={
            "workspace_id": str(tmp_path),
            "agent_instance_id": "external-agent",
            "share_group_id": "g",
        },
    )
    assert forged["ok"] is False
    assert forged["code"] == "trusted_context_capability_required"

    trusted = service.list_external_mcp_servers(
        {"admin": True, "path": str(tmp_path.parent)},
        context=_context(tmp_path),
    )
    assert trusted["ok"] is False
    assert trusted["code"] == "external_mcp_path_forbidden"


def test_list_and_preview_are_redacted_and_non_mutating(tmp_path: Path):
    config = _write_config(
        tmp_path,
        {
            "schema_version": 1,
            "servers": [
                {
                    "server_id": "team-memory",
                    "provider": "acme",
                    "type": "stdio",
                    "descriptor": {
                        "display_name": "Team Memory",
                        "tools": [{"name": "memory_search"}],
                        "memory_entries": [
                            {"body": "TOP SECRET BODY", "metadata": {"kind": "fact"}},
                        ],
                    },
                }
            ],
        },
    )
    before = config.read_bytes()
    service = NativeExternalMCPService(tmp_path)
    context = _context(tmp_path)
    listed = service.dispatch("memoryguard_external_mcp_list", {}, context=context)
    assert listed["status"] == "READY"
    item = listed["servers"][0]
    assert set(("server_ref", "provider", "type", "capabilities")) <= set(item)
    assert "TOP SECRET BODY" not in repr(listed)
    assert str(tmp_path) not in repr(listed)

    preview = service.preview_external_mcp_import(item["server_ref"], context=context)
    assert preview["status"] == "READY"
    assert preview["total"] == 1
    assert "TOP SECRET BODY" not in repr(preview)
    assert "body" not in repr(preview).casefold()
    assert all("content_digest" in entry for entry in preview["preview_entries"])
    assert config.read_bytes() == before


def test_detect_descriptor_is_ephemeral_and_does_not_create_or_write_config(tmp_path: Path):
    service = NativeExternalMCPService(tmp_path)
    result = service.detect_external_mcp(
        "ephemeral",
        {"display_name": "Ephemeral", "tools": [{"name": "dangerous_export"}]},
        context=_context(tmp_path),
    )
    assert result["status"] == "READY"
    assert result["level"] == "L1_unknown_tools"
    assert result["safe_to_auto_call_tools"] is False
    assert not (tmp_path / ".memoryguard").exists()


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ({"schema_version": 2, "servers": []}, "future_external_mcp_schema"),
        ({"schema": "future-v9", "servers": []}, "unknown_external_mcp_schema"),
        ({"servers": [{"server_id": "x", "future_field": "x"}]}, "external_mcp_unknown_field"),
        ({"servers": [{"server_id": "x", "env": {"TOKEN": "x"}}]}, "external_mcp_secret_field"),
    ],
)
def test_future_unknown_and_secret_schema_fail_closed(tmp_path: Path, value: object, code: str):
    _write_config(tmp_path, value)
    result = NativeExternalMCPService(tmp_path).list_external_mcp_servers(context=_context(tmp_path))
    assert result["ok"] is False
    assert result["code"] == code
    assert "TOKEN" not in repr(result)


def test_symlinked_config_and_parent_are_blocked(tmp_path: Path):
    outside = tmp_path.parent / f"external-mcp-outside-{tmp_path.name}"
    outside.mkdir()
    (outside / "servers.json").write_text(json.dumps({"servers": []}), encoding="utf-8")
    link_parent = tmp_path / ".memoryguard"
    try:
        link_parent.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    result = NativeExternalMCPService(tmp_path).list_external_mcp_servers(context=_context(tmp_path))
    assert result["ok"] is False
    assert result["code"] == "reparse_point_blocked"


def test_protected_read_rejects_final_path_swap_without_leaking_outside_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = _write_config(tmp_path, {"servers": [{"server_id": "inside"}]})
    outside = tmp_path.parent / f"external-mcp-race-{tmp_path.name}.json"
    outside.write_text(json.dumps({"servers": [{"server_id": "OUTSIDE_SECRET"}]}), encoding="utf-8")
    original_stat = external_mcp_native.os.stat
    calls = 0

    def swapped_stat(path, *args, **kwargs):
        nonlocal calls
        result = original_stat(path, *args, **kwargs)
        if Path(path) == config:
            calls += 1
            if calls == 1:
                return original_stat(outside, *args, **kwargs)
        return result

    monkeypatch.setattr(external_mcp_native.os, "stat", swapped_stat)
    result = NativeExternalMCPService(tmp_path).list_external_mcp_servers(context=_context(tmp_path))
    assert result["ok"] is False
    assert result["code"] == "external_mcp_path_changed"
    assert "OUTSIDE_SECRET" not in repr(result)


def test_protected_read_rejects_parent_reparse_after_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write_config(tmp_path, {"servers": [{"server_id": "inside"}]})
    original_snapshot = external_mcp_native._snapshot_path_chain
    calls = 0

    def swapped_parent(root: Path, target: Path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise external_mcp_native.ExternalMCPNativeError("reparse_point_blocked")
        return original_snapshot(root, target)

    monkeypatch.setattr(external_mcp_native, "_snapshot_path_chain", swapped_parent)
    result = NativeExternalMCPService(tmp_path).list_external_mcp_servers(context=_context(tmp_path))
    assert result["ok"] is False
    assert result["code"] == "external_mcp_path_changed"


def test_protected_read_rejects_opened_fd_identity_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write_config(tmp_path, {"servers": [{"server_id": "inside"}]})
    original_fstat = external_mcp_native.os.fstat
    calls = 0

    def mismatched_fstat(fd: int):
        nonlocal calls
        info = original_fstat(fd)
        calls += 1
        if calls == 1:
            return os.stat_result((info.st_mode, info.st_ino + 1, info.st_dev, info.st_nlink, info.st_uid, info.st_gid, info.st_size, info.st_atime, info.st_mtime, info.st_ctime))
        return info

    monkeypatch.setattr(external_mcp_native.os, "fstat", mismatched_fstat)
    result = NativeExternalMCPService(tmp_path).list_external_mcp_servers(context=_context(tmp_path))
    assert result["ok"] is False
    assert result["code"] == "external_mcp_path_changed"
    assert "inside" not in repr(result)


@pytest.mark.parametrize(
    ("schema", "code"),
    [
        ({"type": "object", "properties": {1: {"type": "string"}}}, "external_mcp_input_schema_key_type"),
        ({"type": "object", "properties": {"token": {"type": "string"}}}, "external_mcp_secret_field"),
        ({"type": "object", "description": "credential value"}, "external_mcp_secret_field"),
    ],
)
def test_input_schema_rejects_secret_nested_keys_values_and_non_string_keys(tmp_path: Path, schema: object, code: str):
    cap = bind_external_mcp_test_capability(
        servers=[{"server_id": "schema", "tools": [{"name": "safe", "inputSchema": schema}]}],
    )
    result = NativeExternalMCPService(tmp_path, test_capability=cap).list_external_mcp_servers(context=_context(tmp_path))
    assert result["ok"] is False
    assert result["code"] == code


def test_input_schema_limits_depth_nodes_and_serialized_bytes(tmp_path: Path):
    deep: dict[str, object] = {}
    cursor = deep
    for _ in range(external_mcp_native.MAX_INPUT_SCHEMA_DEPTH + 2):
        cursor["properties"] = {}
        cursor = cursor["properties"]  # type: ignore[assignment]
    cap = bind_external_mcp_test_capability(servers=[{"server_id": "deep", "tools": [{"inputSchema": deep}]}])
    result = NativeExternalMCPService(tmp_path, test_capability=cap).list_external_mcp_servers(context=_context(tmp_path))
    assert result["code"] == "external_mcp_input_schema_too_deep"

    huge = {"description": "x" * external_mcp_native.MAX_INPUT_SCHEMA_BYTES}
    cap = bind_external_mcp_test_capability(servers=[{"server_id": "huge", "tools": [{"inputSchema": huge}]}])
    result = NativeExternalMCPService(tmp_path, test_capability=cap).list_external_mcp_servers(context=_context(tmp_path))
    assert result["code"] == "external_mcp_input_schema_too_large"


def test_input_schema_10k_depth_is_rejected_before_json_serialization(tmp_path: Path):
    deep: dict[str, object] = {}
    cursor = deep
    for _ in range(10_000):
        child: dict[str, object] = {}
        cursor["properties"] = child
        cursor = child
    cap = bind_external_mcp_test_capability(
        servers=[{"server_id": "deep-10k", "tools": [{"inputSchema": deep}]}],
    )
    result = NativeExternalMCPService(tmp_path, test_capability=cap).list_external_mcp_servers(
        context=_context(tmp_path),
    )
    assert result["ok"] is False
    assert result["status"] == "BLOCKED"
    assert result["code"] == "external_mcp_input_schema_too_deep"


@pytest.mark.parametrize(
    ("value", "blocked"),
    [
        ("password", True),
        ("passwd", True),
        ("api_key", True),
        ("cookie", True),
        ("command", True),
        ("env", True),
        ("tokenizer", False),
        ("environmental", False),
    ],
)
def test_input_schema_sensitive_value_markers_use_exact_tokens(tmp_path: Path, value: str, blocked: bool):
    cap = bind_external_mcp_test_capability(
        servers=[
            {
                "server_id": "sensitive-value",
                "tools": [{"name": "safe", "inputSchema": {"description": value}}],
            }
        ],
    )
    result = NativeExternalMCPService(tmp_path, test_capability=cap).list_external_mcp_servers(
        context=_context(tmp_path),
    )
    assert result["ok"] is (not blocked)
    if blocked:
        assert result["status"] == "BLOCKED"
        assert result["code"] == "external_mcp_secret_field"


def test_protected_read_rejects_zero_inode_even_with_identical_replayable_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _write_config(tmp_path, {"servers": [{"server_id": "inside"}]})
    original_lstat = external_mcp_native.os.lstat

    def zero_inode(path: Path, *args: object, **kwargs: object):
        info = original_lstat(path, *args, **kwargs)
        fields = list(info)
        fields[1] = 0
        return os.stat_result(fields)

    # Every parent and the target now report identical size/time/mode/dev
    # metadata, but no usable object identity.  The read must fail before any
    # content can cross the boundary.
    monkeypatch.setattr(external_mcp_native.os, "lstat", zero_inode)
    result = NativeExternalMCPService(tmp_path).list_external_mcp_servers(context=_context(tmp_path))
    assert result["ok"] is False
    assert result["status"] == "BLOCKED"
    assert result["code"] == "external_mcp_path_changed"
    assert "inside" not in repr(result)


def test_dependency_injection_requires_process_local_capability_and_typeerror_is_single_call(tmp_path: Path):
    with pytest.raises(NativePortError):
        # The production constructor intentionally has no raw reader seam.
        NativeExternalMCPService(tmp_path, test_capability={"servers": []})  # type: ignore[arg-type]

    calls: list[object] = []

    def reader(_workspace: Path):
        calls.append(_workspace)
        raise TypeError("fixture failure")

    cap = bind_external_mcp_test_capability(reader=reader)
    result = NativeExternalMCPService(tmp_path, test_capability=cap).list_external_mcp_servers(
        context=_context(tmp_path),
    )
    assert result["ok"] is False
    assert result["code"] == "external_mcp_source_unavailable"
    assert len(calls) == 1


def test_scope_filter_uses_bound_context_not_payload_identity(tmp_path: Path):
    _write_config(
        tmp_path,
        {
            "servers": [
                {"server_id": "team", "share_group_id": "other", "tools": [{"name": "x"}]},
                {"server_id": "mine", "share_group_id": "g", "tools": [{"name": "y"}]},
            ]
        },
    )
    service = NativeExternalMCPService(tmp_path)
    result = service.list_external_mcp_servers(context=_context(tmp_path, group="g"))
    assert result["status"] == "READY"
    assert len(result["servers"]) == 1
    assert result["servers"][0]["server_ref"] != "external-mcp:" + ""  # opaque, not caller id


def test_native_port_registry_activates_external_mcp_native_routes(tmp_path: Path):
    coverage = NativeV2RuntimePort(tmp_path).coverage()
    by_surface = {
        surface: {
            item["name"]: item
            for item in coverage["surfaces"][surface]["entries"]
            if "external_mcp" in item["name"]
        }
        for surface in ("mcp", "gui")
    }

    assert by_surface["mcp"]["memoryguard_external_mcp_list"]["status"] == "implemented"
    assert by_surface["mcp"]["memoryguard_external_mcp_list"]["mutation"] is False
    assert by_surface["mcp"]["memoryguard_external_mcp_import"]["status"] == "implemented"
    assert by_surface["mcp"]["memoryguard_external_mcp_import"]["mutation"] is True
    for name in ("list_external_mcp_servers", "preview_external_mcp_import", "detect_external_mcp"):
        assert by_surface["gui"][name]["status"] == "implemented"
        assert by_surface["gui"][name]["mutation"] is False

    # Keep this focused on the phase-owned entries; unrelated registry growth
    # must not make the external MCP contract brittle.
    external_entries = [
        item
        for surface in coverage["surfaces"].values()
        for item in surface["entries"]
        if "external_mcp" in item["name"]
    ]
    assert sum(item["status"] == "implemented" for item in external_entries) == 6
    assert sum(item["status"] == "blocker" for item in external_entries) == 0
    assert coverage["registry_digest"] == coverage["coverage_digest"] == NativeV2RuntimePort(tmp_path).coverage_digest
    assert coverage["counts"]["total"] == sum(surface["total"] for surface in coverage["surfaces"].values())


def test_native_port_external_mcp_reads_use_native_service_and_gui_aliases(tmp_path: Path):
    _write_config(
        tmp_path,
        {
            "schema_version": 1,
            "servers": [
                {
                    "server_id": "team-memory",
                    "provider": "acme",
                    "descriptor": {
                        "display_name": "Team Memory",
                        "tools": [{"name": "memory_search"}],
                        "memory_entries": [{"body": "TOP SECRET BODY", "metadata": {"kind": "fact"}}],
                    },
                }
            ],
        },
    )
    context = _context(tmp_path)
    port = NativeV2RuntimePort(tmp_path)

    listed = port.dispatch_mcp(
        "memoryguard_external_mcp_list", {}, context=context, generation=1,
    )
    assert listed["ok"] is True
    assert listed["data"]["status"] == "READY"
    server_ref = listed["data"]["servers"][0]["server_ref"]
    assert "TOP SECRET BODY" not in repr(listed)

    preview = port.dispatch_gui(
        "preview_external_mcp_import", [server_ref], context=context, generation=1,
    )
    assert preview["ok"] is True
    assert preview["data"]["status"] == "READY"
    assert preview["data"]["total"] == 1
    assert "TOP SECRET BODY" not in repr(preview)

    detected = port.dispatch_gui(
        "detect_external_mcp",
        ["ephemeral", {"display_name": "Ephemeral", "tools": [{"name": "dangerous_export"}]}],
        context=context,
        generation=1,
    )
    assert detected["ok"] is True
    assert detected["data"]["status"] == "READY"
    assert detected["data"]["level"] == "L1_unknown_tools"
    assert detected["data"]["safe_to_auto_call_tools"] is False


def test_native_port_external_mcp_import_persists_static_descriptor_without_calling_tools(tmp_path: Path):
    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 1}

    port = NativeV2RuntimePort(tmp_path, state_provider=Manifest())
    descriptor = {
        "name": "server",
        "tools": [{"name": "unknown_tool", "description": "must never execute"}],
    }
    payload = {"server_id": "server", "descriptor_json": json.dumps(descriptor)}
    first = port.dispatch_mcp(
        "memoryguard_external_mcp_import", payload,
        context=_context(tmp_path), generation=1, state="V2_ACTIVE",
    )
    assert first["ok"] is True, first
    assert first["data"]["level"] == "L1_unknown_tools"
    assert first["data"]["safe_to_auto_call_tools"] is False
    assert first["data"]["unknown_tools_called"] is False
    assert first["data"]["updated_existing"] is False

    second = port.dispatch_mcp(
        "memoryguard_external_mcp_import", payload,
        context=_context(tmp_path), generation=1, state="V2_ACTIVE",
    )
    assert second["ok"] is True
    assert second["data"]["updated_existing"] is True
    listed = port.dispatch_mcp(
        "memoryguard_external_mcp_list", {}, context=_context(tmp_path), generation=1,
    )
    assert listed["ok"] is True
    assert listed["data"]["total"] == 1
    assert listed["data"]["servers"][0]["capabilities"]["unknown_tools_called"] is False
    assert (tmp_path / ".memoryguard" / "external-mcp" / "servers.json").is_file()
