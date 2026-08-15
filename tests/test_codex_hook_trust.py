from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from memoryguard import codex_hook_trust as trust
from memoryguard.host_hooks import HostHookManager


EVENTS = trust.EXPECTED_EVENTS


def _command(event: str) -> str:
    return (
        "python -X utf8 -m memoryguard.host_hooks run "
        f"--provider codex --event {event} "
        "--workspace C:\\control "
        "--agent-id agent-a --share-group-id group-a "
        "--managed-by memoryguard"
    )


def _metadata(hooks_path: Path, *, enabled: bool, trusted: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, (event_name, cli_event) in enumerate(EVENTS.items()):
        rows.append(
            {
                "key": f"{hooks_path}:{cli_event}:{index}:0",
                "eventName": event_name,
                "handlerType": "command",
                "executionMode": "sync",
                "matcher": None,
                "command": _command(cli_event),
                "timeoutSec": 15,
                "sourcePath": str(hooks_path),
                "source": "user",
                "displayOrder": index,
                "enabled": enabled,
                "isManaged": False,
                "currentHash": f"sha256:{index:064x}",
                "trustStatus": "trusted" if trusted else "modified",
            }
        )
    return rows


def _hooks_response(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "result": {
            "data": [
                {
                    "cwd": "C:\\control",
                    "warnings": [],
                    "errors": [],
                    "hooks": rows,
                }
            ]
        }
    }


def _config_response(
    config_path: Path,
    rows: list[dict[str, Any]],
    *,
    enabled: bool,
    trusted_hashes: bool,
    version: str = "sha256:user-v1",
) -> dict[str, Any]:
    state: dict[str, dict[str, Any]] = {
        "unrelated.hook:0:0": {
            "enabled": False,
            "trusted_hash": "sha256:unrelated",
        }
    }
    for row in rows:
        state[row["key"]] = {
            "enabled": enabled,
            "trusted_hash": row["currentHash"] if trusted_hashes else "sha256:old",
        }
    return {
        "result": {
            "config": {"hooks": {"state": state}},
            "layers": [
                {
                    "name": {
                        "type": "user",
                        "file": str(config_path),
                        "profile": None,
                    },
                    "version": version,
                    "disabledReason": None,
                }
            ],
            "origins": {},
        }
    }


class FakeClient:
    def __init__(
        self,
        *,
        before_rows: list[dict[str, Any]],
        after_rows: list[dict[str, Any]],
        config_response: dict[str, Any],
    ) -> None:
        self.before_rows = before_rows
        self.after_rows = after_rows
        self.config_response = config_response
        self.requests: list[tuple[str, Any]] = []
        self.hooks_calls = 0
        self.closed = False

    def request(self, method: str, params: Any) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "hooks/list":
            self.hooks_calls += 1
            return _hooks_response(
                self.before_rows if self.hooks_calls == 1 else self.after_rows
            )
        if method == "config/read":
            return self.config_response
        if method == "config/batchWrite":
            return {
                "result": {
                    "status": "ok",
                    "version": "sha256:user-v2",
                    "filePath": params["filePath"],
                    "overriddenMetadata": None,
                }
            }
        raise AssertionError(method)

    def close(self) -> None:
        self.closed = True


def _setup_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    hooks_path = codex_home / "hooks.json"
    config_path = codex_home / "config.toml"
    hooks_path.write_text("{}", encoding="utf-8")
    config_path.write_text("[hooks.state]\n", encoding="utf-8")
    cli = tmp_path / "codex.exe"
    cli.write_bytes(b"fake")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return hooks_path, config_path, cli


def test_inspect_reports_disabled_and_modified_events_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    hooks_path, _config_path, cli = _setup_paths(tmp_path, monkeypatch)
    rows = _metadata(hooks_path, enabled=False, trusted=False)
    fake = FakeClient(
        before_rows=rows,
        after_rows=rows,
        config_response={"result": {}},
    )

    result = trust.inspect_codex_memoryguard_hooks(
        cwd=tmp_path,
        codex_cli=cli,
        client_factory=lambda _cli, _timeout: fake,
    )

    assert result["status"] == "degraded"
    assert result["verified"] is False
    assert result["managed_count"] == len(EVENTS)
    assert result["trusted_count"] == 0
    assert "disabled=" in result["detail"]
    assert "untrusted=" in result["detail"]
    assert fake.requests == [("hooks/list", {"cwds": [str(tmp_path)]})]
    assert fake.closed is True


def test_inspect_reports_exact_trusted_enabled_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    hooks_path, _config_path, cli = _setup_paths(tmp_path, monkeypatch)
    rows = _metadata(hooks_path, enabled=True, trusted=True)
    fake = FakeClient(
        before_rows=rows,
        after_rows=rows,
        config_response={"result": {}},
    )

    result = trust.inspect_codex_memoryguard_hooks(
        cwd=tmp_path,
        codex_cli=cli,
        client_factory=lambda _cli, _timeout: fake,
    )

    assert result["status"] == "ok"
    assert result["reason"] == "trusted_and_enabled"
    assert result["verified"] is True
    assert result["managed_count"] == len(EVENTS)
    assert result["trusted_count"] == len(EVENTS)


def test_reconcile_enables_and_trusts_only_exact_memoryguard_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    hooks_path, config_path, cli = _setup_paths(tmp_path, monkeypatch)
    before = _metadata(hooks_path, enabled=False, trusted=False)
    after = _metadata(hooks_path, enabled=True, trusted=True)
    fake = FakeClient(
        before_rows=before,
        after_rows=after,
        config_response=_config_response(
            config_path,
            before,
            enabled=False,
            trusted_hashes=False,
        ),
    )

    result = trust.reconcile_codex_memoryguard_hooks(
        cwd=tmp_path,
        enabled=True,
        codex_cli=cli,
        client_factory=lambda _cli, _timeout: fake,
    )

    assert result["status"] == "ok"
    assert result["verified"] is True
    assert result["managed_count"] == len(EVENTS)
    assert result["trusted_count"] == len(EVENTS)
    assert result["changed_count"] == 2 * len(EVENTS)
    assert fake.closed is True

    writes = [params for method, params in fake.requests if method == "config/batchWrite"]
    assert len(writes) == 1
    write = writes[0]
    assert write["expectedVersion"] == "sha256:user-v1"
    assert write["filePath"] == str(config_path)
    assert write["reloadUserConfig"] is True
    assert len(write["edits"]) == 2 * len(EVENTS)
    serialized = json.dumps(write["edits"], ensure_ascii=False)
    assert "unrelated.hook" not in serialized
    edited_keys = {
        json.loads(edit["keyPath"][len("hooks.state.") : edit["keyPath"].rfind(".")])
        for edit in write["edits"]
    }
    assert edited_keys == {row["key"] for row in before}
    for row in before:
        assert row["currentHash"] in serialized


def test_reconcile_is_idempotent_when_hooks_are_already_trusted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    hooks_path, config_path, cli = _setup_paths(tmp_path, monkeypatch)
    rows = _metadata(hooks_path, enabled=True, trusted=True)
    fake = FakeClient(
        before_rows=rows,
        after_rows=rows,
        config_response=_config_response(
            config_path,
            rows,
            enabled=True,
            trusted_hashes=True,
        ),
    )

    result = trust.reconcile_codex_memoryguard_hooks(
        cwd=tmp_path,
        enabled=True,
        codex_cli=cli,
        client_factory=lambda _cli, _timeout: fake,
    )

    assert result["verified"] is True
    assert result["reason"] == "already_current"
    assert result["changed_count"] == 0
    assert not any(method == "config/batchWrite" for method, _ in fake.requests)


def test_reconcile_refuses_duplicate_managed_event_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    hooks_path, config_path, cli = _setup_paths(tmp_path, monkeypatch)
    rows = _metadata(hooks_path, enabled=False, trusted=False)
    duplicate = dict(rows[0])
    duplicate["key"] += ":duplicate"
    rows.append(duplicate)
    fake = FakeClient(
        before_rows=rows,
        after_rows=rows,
        config_response=_config_response(
            config_path,
            rows,
            enabled=False,
            trusted_hashes=False,
        ),
    )

    result = trust.reconcile_codex_memoryguard_hooks(
        cwd=tmp_path,
        enabled=True,
        codex_cli=cli,
        client_factory=lambda _cli, _timeout: fake,
    )

    assert result["verified"] is False
    assert result["reason"] == "managed_hook_set_incomplete"
    assert "duplicates=sessionStart" in result["detail"]
    assert not any(method == "config/batchWrite" for method, _ in fake.requests)


def test_reconcile_ignores_unrelated_and_spoofed_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    hooks_path, config_path, cli = _setup_paths(tmp_path, monkeypatch)
    rows = _metadata(hooks_path, enabled=True, trusted=True)
    rows.extend(
        [
            {
                **rows[0],
                "key": "other-project-hook",
                "sourcePath": str(tmp_path / "project" / ".codex" / "hooks.json"),
            },
            {
                **rows[1],
                "key": "spoofed-command",
                "command": "python user-hook.py --managed-by memoryguard",
            },
            {
                **rows[2],
                "key": "wrong-provider",
                "command": _command("user_prompt").replace(
                    "--provider codex", "--provider cursor"
                ),
            },
        ]
    )
    fake = FakeClient(
        before_rows=rows,
        after_rows=rows,
        config_response=_config_response(
            config_path,
            rows[: len(EVENTS)],
            enabled=True,
            trusted_hashes=True,
        ),
    )

    result = trust.reconcile_codex_memoryguard_hooks(
        cwd=tmp_path,
        enabled=True,
        codex_cli=cli,
        client_factory=lambda _cli, _timeout: fake,
    )

    assert result["verified"] is True
    assert result["managed_count"] == len(EVENTS)
    assert result["changed_count"] == 0


def _write_generated_hooks(
    hooks_path: Path,
    *,
    bindings: list[tuple[str, str, str]],
    omit_event: str = "",
) -> None:
    hooks: dict[str, list[dict[str, Any]]] = {}
    for binding_index, (workspace, agent_id, group_id) in enumerate(bindings):
        for event_name, cli_event in EVENTS.items():
            if event_name == omit_event:
                continue
            command = (
                "python -X utf8 -m memoryguard.host_hooks run "
                f"--provider codex --event {cli_event} "
                f"--workspace {json.dumps(workspace)} "
                f"--agent-id {agent_id} --share-group-id {group_id} "
                "--managed-by memoryguard"
            )
            hooks.setdefault(event_name[0].upper() + event_name[1:], []).append(
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "commandWindows": command,
                            "timeout": 15,
                            "bindingIndex": binding_index,
                        }
                    ],
                }
            )
    hooks_path.write_text(
        json.dumps({"hooks": hooks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def test_ensure_existing_binding_upgrades_old_seven_event_hook_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    hooks_path, _config_path, cli = _setup_paths(tmp_path, monkeypatch)
    workspace = str(tmp_path / "control")
    _write_generated_hooks(
        hooks_path,
        bindings=[(workspace, "agent-a", "group-a")],
        omit_event="subagentStop",
    )
    calls: list[dict[str, Any]] = []

    class FakeManager:
        def install(self, provider: str, **kwargs: Any) -> dict[str, Any]:
            calls.append({"provider": provider, **kwargs})
            return {"configured": True, "status": "configured_pending_runtime"}

    monkeypatch.setattr(
        trust,
        "reconcile_codex_memoryguard_hooks",
        lambda **kwargs: {
            "status": "ok",
            "verified": True,
            "managed_count": len(EVENTS),
            "args": kwargs,
        },
    )

    result = trust.ensure_existing_codex_memoryguard_hooks(
        codex_cli=cli,
        manager_factory=lambda _workspace: FakeManager(),
        mode_getter=lambda _workspace, _provider, _agent: "enforce",
    )

    assert result["verified"] is True
    assert result["reason"] == "ensured"
    assert calls == [
        {
            "provider": "codex",
            "agent_instance_id": "agent-a",
            "share_group_id": "group-a",
            "mode": "enforce",
            "reconcile_trust": False,
        }
    ]
    assert result["hook_trust"]["managed_count"] == len(EVENTS)


def test_ensure_existing_binding_refuses_ambiguous_bindings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    hooks_path, _config_path, cli = _setup_paths(tmp_path, monkeypatch)
    _write_generated_hooks(
        hooks_path,
        bindings=[
            (str(tmp_path / "one"), "agent-a", "group-a"),
            (str(tmp_path / "two"), "agent-b", "group-b"),
        ],
    )
    called = False

    def manager_factory(_workspace: str | Path) -> Any:
        nonlocal called
        called = True
        raise AssertionError("must not create manager for ambiguous binding")

    result = trust.ensure_existing_codex_memoryguard_hooks(
        codex_cli=cli,
        manager_factory=manager_factory,
        mode_getter=lambda *_args: "enforce",
    )

    assert result["verified"] is False
    assert result["reason"] == "managed_hook_binding_ambiguous"
    assert called is False


def test_host_hook_manager_reconciles_codex_only_when_explicitly_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    manager = HostHookManager(tmp_path)

    class FakeAdapter:
        def install(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "provider": "codex",
                "configured": True,
                "trust_required": True,
            }

    monkeypatch.setattr(manager, "adapter", lambda _provider: FakeAdapter())
    calls: list[dict[str, Any]] = []

    def fake_reconcile(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "status": "ok",
            "verified": True,
            "managed_count": len(EVENTS),
            "changed_count": 2 * len(EVENTS),
        }

    monkeypatch.setattr(trust, "reconcile_codex_memoryguard_hooks", fake_reconcile)

    result = manager.install(
        "codex",
        agent_instance_id="agent-a",
        share_group_id="group-a",
        reconcile_trust=True,
    )

    assert calls == [{"cwd": manager.workspace, "enabled": True}]
    assert result["hook_trust"]["verified"] is True
    assert result["trust_required"] is False


def test_host_hook_manager_status_surfaces_disabled_codex_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    manager = HostHookManager(tmp_path)

    class FakeAdapter:
        def status(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "provider": "codex",
                "configured": True,
                "status": "operational",
                "runtime_verified": True,
            }

    monkeypatch.setattr(manager, "adapter", lambda _provider: FakeAdapter())
    monkeypatch.setattr(
        trust,
        "inspect_codex_memoryguard_hooks",
        lambda **_kwargs: {
            "status": "degraded",
            "verified": False,
            "reason": "hook_runtime_state_invalid",
            "managed_count": len(EVENTS),
        },
    )

    result = manager.status("codex", inspect_trust=True)

    assert result["status"] == "configured_untrusted"
    assert result["runtime_verified"] is False
    assert result["trust_required"] is True
    assert result["hook_trust"]["reason"] == "hook_runtime_state_invalid"


def test_host_hook_manager_status_keeps_operational_when_trust_is_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    manager = HostHookManager(tmp_path)

    class FakeAdapter:
        def status(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "provider": "codex",
                "configured": True,
                "status": "operational",
                "runtime_verified": True,
            }

    monkeypatch.setattr(manager, "adapter", lambda _provider: FakeAdapter())
    monkeypatch.setattr(
        trust,
        "inspect_codex_memoryguard_hooks",
        lambda **_kwargs: {
            "status": "ok",
            "verified": True,
            "reason": "trusted_and_enabled",
            "managed_count": len(EVENTS),
        },
    )

    result = manager.status("codex", inspect_trust=True)

    assert result["status"] == "operational"
    assert result["runtime_verified"] is True
    assert result["trust_required"] is False


def test_host_hook_manager_low_level_install_does_not_launch_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    manager = HostHookManager(tmp_path)

    class FakeAdapter:
        def install(self, **_kwargs: Any) -> dict[str, Any]:
            return {"provider": "codex", "configured": True}

    monkeypatch.setattr(manager, "adapter", lambda _provider: FakeAdapter())
    monkeypatch.setattr(
        trust,
        "reconcile_codex_memoryguard_hooks",
        lambda **_kwargs: pytest.fail("unexpected reconcile"),
    )

    result = manager.install(
        "codex",
        agent_instance_id="agent-a",
        share_group_id="group-a",
    )

    assert "hook_trust" not in result


def test_disable_changes_only_enabled_leaves_and_does_not_retrust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    hooks_path, config_path, cli = _setup_paths(tmp_path, monkeypatch)
    before = _metadata(hooks_path, enabled=True, trusted=True)
    after = _metadata(hooks_path, enabled=False, trusted=True)
    fake = FakeClient(
        before_rows=before,
        after_rows=after,
        config_response=_config_response(
            config_path,
            before,
            enabled=True,
            trusted_hashes=True,
        ),
    )

    result = trust.reconcile_codex_memoryguard_hooks(
        cwd=tmp_path,
        enabled=False,
        codex_cli=cli,
        client_factory=lambda _cli, _timeout: fake,
    )

    assert result["verified"] is True
    assert result["changed_count"] == len(EVENTS)
    write = next(params for method, params in fake.requests if method == "config/batchWrite")
    assert all(edit["keyPath"].endswith(".enabled") for edit in write["edits"])
    assert all(edit["value"] is False for edit in write["edits"])
