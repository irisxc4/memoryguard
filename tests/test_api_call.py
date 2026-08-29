"""GovernanceApi smoke tests."""
from datetime import datetime
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.gui import GovernanceApi  # noqa: E402
from memoryguard.access_context import AccessContext  # noqa: E402
from memoryguard.migration.upgrade import run_upgrade  # noqa: E402
from memoryguard.runtime_v2.group_native import (  # noqa: E402
    GroupControlService,
    personal_group_id,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _active_gui_api(root: Path) -> GovernanceApi:
    """Use the public upgrade contract before entering the V2 GUI surface."""
    ready = run_upgrade(root, data_home=root, apply=True)
    assert ready["status"] == "V2_READY", ready
    active = run_upgrade(
        root,
        data_home=root,
        apply=True,
        confirm="V2_ACTIVE",
    )
    assert active["v2_active"] is True, active

    agent = "api-test-agent"
    group = personal_group_id(agent)
    GroupControlService(root, write=True).bind_agent(agent, group)
    return GovernanceApi(
        str(root),
        _trusted_access_context=AccessContext(
            trusted_agent_id=agent,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="api-test-session",
            session_source="transport",
            session_trusted=True,
        ),
    )


def test_run_audit_returns_v2_report_summary(tmp_path) -> None:
    api = _active_gui_api(tmp_path)

    result = api.run_audit()

    assert result["ok"] is True
    assert result["path"] == "v2"
    assert isinstance(result["data"]["domains"], list)
    assert isinstance(result["data"]["blocker_codes"], list)
    assert isinstance(result["data"]["candidate_count"], int)


def test_run_audit_and_get_audit_carry_completion_evidence(tmp_path) -> None:
    api = _active_gui_api(tmp_path)

    for result in (api.run_audit(), api.get_audit()):
        assert result["ok"] is True
        data = result["data"]
        assert data["audit_state"] == "completed"
        stamp = data.get("generated_at") or data.get("completed_at")
        parsed = datetime.fromisoformat(str(stamp))
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() is not None
        assert data["status"] in {"PASS", "BLOCKED"}
        assert isinstance(data["blocked"], bool)
        assert isinstance(data["blockers"], list)
        assert isinstance(data["blocker_codes"], list)
        assert data["health_model"] == "v2_reference_integrity"
        assert data["health_model_version"] == 1
        assert data["health_scope"] == "reference_integrity"
        assert data["health_available"] is True
        assert data["health_status"] == "available"
        assert data["health_score"] == 100.0
        assert set(data["health_components"]) == {
            "schema", "storage_integrity", "references", "delivery"
        }
        assert data["health_evidence"]["audit_status"] == "PASS"


def test_get_neuron_graph_supports_empty_projection(tmp_path) -> None:
    api = _active_gui_api(tmp_path)

    graph = api.get_neuron_graph()

    assert graph["ok"] is True
    assert graph["path"] == "v2"
    assert graph["data"]["status"] == "NO_SOURCE"
    assert graph["data"]["base_empty"] is True
    assert graph["data"]["virtual_overlay_available"] is True
    node_ids = {item["id"] for item in graph["data"]["nodes"]}
    assert {"main", "virtual-rules-habits", "virtual-conversation-history"} <= node_ids
    assert any(item.get("edge_type") == "virtual_index" for item in graph["data"]["edges"])


def test_pyproject_declares_windowed_gui_entry() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "[project.gui-scripts]" in pyproject
    assert 'memoryguard-gui = "memoryguard.cli:gui_main"' in pyproject
    assert (PROJECT_ROOT / "MemoryGuard.pyw").exists()
    assert 'memoryguard = ["static/*.js", "static/*.png", "static/*.ico"]' in pyproject


def test_safe_bridge_preserves_trusted_gui_context(tmp_path) -> None:
    from memoryguard.access_context import AccessContext
    from memoryguard.gui import SafeBridgeApi

    context = AccessContext(
        trusted_agent_id="gui",
        is_admin=True,
        strict_binding=True,
        allow_anon=False,
        session_id="session",
        session_source="transport",
        session_trusted=True,
    )
    bridge = SafeBridgeApi(str(tmp_path), _trusted_access_context=context)

    assert bridge._trusted_access_context is context
    assert bridge._trusted_access_context.require_admin() == (True, "")


def test_native_window_uses_packaged_brand_icon(monkeypatch) -> None:
    from memoryguard import gui

    calls = {}

    class FakeWebview:
        @staticmethod
        def create_window(**kwargs):
            calls["window"] = kwargs

        @staticmethod
        def start(**kwargs):
            calls["start"] = kwargs

    monkeypatch.setitem(sys.modules, "webview", FakeWebview)
    result = gui.open_native_window("<html></html>")

    assert result == 0
    icon_path = Path(calls["start"]["icon"])
    assert icon_path.is_file()
    assert icon_path.name == (
        "memoryguard-icon.ico" if sys.platform == "win32"
        else "memoryguard-icon.png"
    )


def test_windows_taskbar_identity_is_set_before_window_creation(monkeypatch) -> None:
    from memoryguard import gui

    calls = []

    class FakeWebview:
        @staticmethod
        def create_window(**kwargs):
            calls.append("create")

        @staticmethod
        def start(**kwargs):
            calls.append("start")

    monkeypatch.setitem(sys.modules, "webview", FakeWebview)
    monkeypatch.setattr(
        gui,
        "_set_windows_app_user_model_id",
        lambda: calls.append("app-id") or True,
    )

    assert gui.open_native_window("<html></html>") == 0
    assert calls == ["app-id", "create", "start"]


def test_gui_main_falls_back_to_interactive_localhost(monkeypatch, tmp_path) -> None:
    from memoryguard import cli
    from memoryguard import gui

    called = {}
    monkeypatch.setattr(gui, "has_native_gui", lambda: False)

    def fake_localhost(workspace: str, *, auto_open: bool = True):
        called["workspace"] = workspace
        called["auto_open"] = auto_open
        return 0, "http://127.0.0.1:12345/"

    monkeypatch.setattr(gui, "open_localhost_window", fake_localhost)

    result = cli.gui_main([str(tmp_path)])

    assert result == 0
    assert called == {
        "workspace": str(tmp_path.resolve()),
        "auto_open": True,
    }


@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows path semantics: C:\\Windows does not exist on POSIX runners",
)
def test_gui_main_rejects_windows_system_directory(monkeypatch, capsys) -> None:
    from memoryguard import cli

    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    result = cli.gui_main([r"C:\Windows\System32"])

    assert result == 2
    assert "refusing to use a Windows system directory" in capsys.readouterr().err


def test_bare_gui_ignores_project_workspace_environment(monkeypatch, tmp_path) -> None:
    from memoryguard.cli import _resolve_gui_workspace

    project = tmp_path / "project"
    data_home = tmp_path / "global-home"
    project.mkdir()
    data_home.mkdir()
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(project))
    monkeypatch.setenv("MEMORYGUARD_HOME", str(data_home))

    assert _resolve_gui_workspace([]) == data_home.resolve()


def test_bare_gui_uses_fixed_user_control_directory_without_picker(
    monkeypatch, tmp_path,
) -> None:
    from memoryguard import cli

    control_home = tmp_path / "memoryguard-home"
    stale_project = tmp_path / "stale-project"
    stale_project.mkdir()
    control_home.mkdir()
    (control_home / "gui-state.json").write_text(
        '{"workspace": "%s"}' % stale_project,
        encoding="utf-8",
    )
    monkeypatch.setenv("MEMORYGUARD_HOME", str(control_home))
    monkeypatch.delenv("MEMORYGUARD_WORKSPACE", raising=False)

    assert cli._resolve_gui_workspace([]) == control_home.resolve()


def test_bare_gui_creates_fixed_control_directory(
    monkeypatch, tmp_path,
) -> None:
    from memoryguard import cli
    from memoryguard import gui

    control_home = tmp_path / "memoryguard-home"
    monkeypatch.setenv("MEMORYGUARD_HOME", str(control_home))
    monkeypatch.delenv("MEMORYGUARD_WORKSPACE", raising=False)
    monkeypatch.setattr(gui, "has_native_gui", lambda: False)

    called = {}

    def fake_localhost(workspace: str, *, auto_open: bool = True):
        called["workspace"] = workspace
        called["auto_open"] = auto_open
        return 0, "http://127.0.0.1:12345/"

    monkeypatch.setattr(gui, "open_localhost_window", fake_localhost)
    assert cli.gui_main([]) == 0
    assert control_home.is_dir()
    assert called == {
        "workspace": str(control_home.resolve()),
        "auto_open": True,
    }
