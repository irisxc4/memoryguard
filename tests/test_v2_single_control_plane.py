"""Phase 0 regressions for the single V2 control plane."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from argparse import Namespace
from pathlib import Path

import pytest

from memoryguard import host_hooks
from memoryguard.cli import _cli_workspace, _resolve_gui_workspace, build_parser
from memoryguard.cutover_v2.facade import V2RuntimeFacade
from memoryguard.data_home import resolve_data_home
from memoryguard.mcp_server import _resolve_memory_workspace
from memoryguard.migration.upgrade import run_upgrade
from memoryguard.rule_scope import canonical_project_ref
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.storage.layout import WorkspaceV2Layout


def _v1_project(root: Path) -> Path:
    legacy = root / ".memoryguard" / "shared-memory" / "legacy-group"
    legacy.mkdir(parents=True)
    (legacy / "memory.db").write_bytes(b"v1 marker")
    (root / ".memoryguard" / "agent-bindings").mkdir(parents=True)
    (root / ".memoryguard" / "agent-bindings" / "legacy.json").write_text(
        json.dumps({"binding_id": "legacy-binding", "status": "active"}),
        encoding="utf-8",
    )
    return root


class _ActiveHookFacade:
    def __init__(self, seen_contexts: list[object]) -> None:
        self.seen_contexts = seen_contexts

    def state_snapshot(self):
        return {"state": "V2_ACTIVE", "generation": 3}

    def bootstrap_hook(self, request=None, payload=None, *, context=None, snapshot=None):
        del request, payload, snapshot
        self.seen_contexts.append(context)
        return {
            "ok": True,
            "status": "ok",
            "data": {
                "mandatory": [],
                "relevant": [],
                "reference_only": [],
                "ready": True,
                "state": "V2_ACTIVE",
                "status": "ok",
            },
        }


class _ActiveManifest:
    def current(self):
        return {"state": "V2_ACTIVE", "generation": 3}


class _NativePort:
    supports_rule_mutation_context = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def dispatch(self, surface, name, args, **kwargs):
        del args, kwargs
        self.calls.append((surface, name))
        return {"ok": True, "status": "ok", "data": {}}


def test_v1_project_is_only_project_ref_not_control_plane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _v1_project(tmp_path / "project")
    data_home = tmp_path / "data-home"
    data_home.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(data_home))
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(project))

    assert resolve_data_home() == data_home.resolve()
    assert _resolve_gui_workspace([]) == data_home.resolve()
    assert _resolve_memory_workspace({}) == data_home.resolve()
    assert _resolve_memory_workspace({"workspace": str(project)}) == data_home.resolve()

    for argv in (("doctor",), ("mcp-status",), ("hooks", "status"), ("desktop",)):
        args = build_parser().parse_args(list(argv))
        assert _cli_workspace(args) == data_home.resolve(), argv

    seen_workspaces: list[Path] = []
    seen_contexts: list[object] = []

    def factory(workspace):
        seen_workspaces.append(Path(workspace).resolve())
        return _ActiveHookFacade(seen_contexts)

    monkeypatch.setattr(host_hooks, "_v2_runtime_facade_factory", factory)
    result = host_hooks.run_hook(
        provider="claude",
        event="session_start",
        # Installed Hook commands carry the canonical control root; payload
        # cwd remains the project-only scope reference.
        workspace=data_home,
        agent_instance_id="agent-a",
        share_group_id="group-a",
        payload={"cwd": str(project), "session_id": "session-a"},
    )

    assert "v2_upgrade_required" not in json.dumps(result)
    assert seen_workspaces == [data_home.resolve()]
    assert seen_contexts
    context = seen_contexts[0]
    assert context["workspace_id"] == str(data_home.resolve())
    assert context["project_ref"] == canonical_project_ref(str(project))


def test_ordinary_surfaces_use_v2_without_a_v1_store_route() -> None:
    port = _NativePort()

    class ForbiddenV1Store:
        def __init__(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("V1 store was instantiated")

    facade = V2RuntimeFacade(
        manifest=_ActiveManifest(),
        v2=port,
        legacy_store=ForbiddenV1Store,
    )

    results = (
        facade.dispatch_gui("get_storage_overview"),
        facade.dispatch_mcp("memoryguard_memory_status"),
        facade.dispatch_cli("doctor"),
        facade.bootstrap_hook({"task": "fresh session"}),
    )
    assert all(result.get("path") == "v2" for result in results)
    assert all(result.get("code") != "v2_upgrade_required" for result in results)
    assert {surface for surface, _ in port.calls} == {"gui", "mcp", "cli", "hook"}


def _legacy_upgrade_fixture(root: Path) -> None:
    group_db = root / ".memoryguard" / "shared-memory" / "shared-team" / "memory.db"
    group_db.parent.mkdir(parents=True, exist_ok=True)
    body = "keep this V1 memory"
    conn = sqlite3.connect(group_db)
    try:
        conn.execute(
            "CREATE TABLE records(memory_id TEXT PRIMARY KEY, body TEXT, kind TEXT, status TEXT, "
            "confidence REAL, locked INTEGER, injection_policy TEXT, priority INTEGER, supersedes TEXT, "
            "provenance TEXT, agent_instance_id TEXT, created_at TEXT, updated_at TEXT, canonical_hash TEXT, "
            "dedup_domain TEXT)"
        )
        conn.execute(
            "CREATE TABLE rule_assignments(memory_id TEXT, target_type TEXT, target_id TEXT, project_ref TEXT, "
            "effect TEXT, priority_override INTEGER, created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "memory-1", body, "fact", "active", 0.9, 1, "always", 2, "[]", "[]",
                "agent-1", "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00",
                hashlib.sha256(body.encode()).hexdigest(), "relevant",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    binding = root / ".memoryguard" / "agent-bindings" / "binding-1.json"
    binding.parent.mkdir(parents=True, exist_ok=True)
    binding.write_text(
        json.dumps({
            "binding_id": "binding-1",
            "agent_instance_id": "agent-1",
            "share_group_id": "shared-team",
            "mcp_server_name": "memoryguard",
            "native_memory_mode": "observed",
            "status": "active",
            "redirect_paths": [],
        }),
        encoding="utf-8",
    )


def test_successful_upgrade_cleans_v1_hooks_and_artifacts_but_preserves_v2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "memoryguard-home"
    _legacy_upgrade_fixture(root)

    fake_home = tmp_path / "host-home"
    codex = fake_home / ".codex"
    codex.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    generated = host_hooks._command(
        "codex", "session_start", root, "agent-1", "shared-team", windows=False
    )
    old_generated = generated.replace(" --share-group-id shared-team", "")
    user_owned = "python -m user-owned-hook"
    (codex / "hooks.json").write_text(
        json.dumps({"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": generated}]},
            {"hooks": [{"type": "command", "command": old_generated}]},
            {"hooks": [{"type": "command", "command": user_owned}]},
        ]}}),
        encoding="utf-8",
    )

    report = run_upgrade(
        root, data_home=root, apply=True, confirm="V2_ACTIVE"
    )
    assert report["ok"] is True, report
    assert report["status"] == "V2_ACTIVE"

    hook_data = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))
    commands = [
        item["command"]
        for entries in hook_data.get("hooks", {}).values()
        for entry in entries
        for item in entry.get("hooks", [])
        if isinstance(item, dict) and "command" in item
    ]
    assert user_owned in commands
    assert generated not in commands
    assert old_generated not in commands
    cleanup_debug = json.dumps(report.get("cleanup"), ensure_ascii=False, indent=2)
    assert not (root / ".memoryguard" / "shared-memory").exists(), cleanup_debug
    assert not (root / ".memoryguard" / "agent-bindings").exists(), cleanup_debug
    layout = WorkspaceV2Layout(root)
    assert layout.manifest_db.exists()
    assert layout.memory_db.exists()
    assert GroupControlService(root, write=False).active_binding_for_agent("agent-1")


def test_upgrade_uses_user_data_home_as_v2_target_and_v1_as_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "legacy-project"
    target = tmp_path / "user-data-home"
    _legacy_upgrade_fixture(source)
    preserved_v1 = (
        source / ".memoryguard" / "shared-memory" / "shared-team" / "unmigrated.bin"
    )
    preserved_v1.write_bytes(b"leave this source artifact")

    fake_home = tmp_path / "host-home"
    codex = fake_home / ".codex"
    codex.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    source_hook = host_hooks._command(
        "codex", "session_start", source, "agent-1", "shared-team", windows=False
    )
    old_source_hook = source_hook.replace(" --share-group-id shared-team", "")
    valid_v2_hook = host_hooks._command(
        "codex", "session_start", target, "agent-1", "shared-team", windows=False
    )
    (codex / "hooks.json").write_text(
        json.dumps({"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": old_source_hook}]},
            {"hooks": [{"type": "command", "command": valid_v2_hook}]},
        ]}}),
        encoding="utf-8",
    )

    report = run_upgrade(
        target, data_home=source, apply=True, confirm="V2_ACTIVE"
    )
    assert report["ok"] is True, report
    assert report["workspace"] == str(target.resolve())
    assert report["data_home"] == str(source.resolve())

    hook_data = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))
    commands = [
        item["command"]
        for entries in hook_data.get("hooks", {}).values()
        for entry in entries
        for item in entry.get("hooks", [])
        if isinstance(item, dict) and "command" in item
    ]
    assert old_source_hook not in commands
    assert valid_v2_hook in commands
    assert preserved_v1.exists()
    assert not (source / ".memoryguard" / "shared-memory" / "shared-team" / "memory.db").exists()
    assert not (source / ".memoryguard" / "agent-bindings").exists()
    assert GroupControlService(target, write=False).active_binding_for_agent("agent-1")

    replay = run_upgrade(target, data_home=source, apply=True)
    assert replay["ok"] is True, replay
    assert replay["status"] == "V2_ACTIVE"
    assert replay["cleanup"]["status"] == "NOOP"
