"""GovernanceApi smoke tests."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.gui import GovernanceApi  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_WORKSPACE = Path(__file__).resolve().parent / "fixtures" / "workspace"


def test_run_audit_returns_report_summary() -> None:
    api = GovernanceApi(str(FIXTURE_WORKSPACE))

    result = api.run_audit()

    assert "summary" in result
    assert "findings" in result
    assert "health_score" in result
    assert isinstance(result["findings"], list)


def test_get_neuron_graph_supports_empty_projection() -> None:
    api = GovernanceApi(str(FIXTURE_WORKSPACE))

    graph = api.get_neuron_graph()

    assert isinstance(graph, dict)
    assert graph.get("empty") is True
    assert graph.get("reason") == "missing_governance_scope" or graph.get("error") == "missing_governance_scope"


def test_pyproject_declares_windowed_gui_entry() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "[project.gui-scripts]" in pyproject
    assert 'memoryguard-gui = "memoryguard.cli:gui_main"' in pyproject
    assert (PROJECT_ROOT / "MemoryGuard.pyw").exists()
    assert 'memoryguard = ["static/*.js", "static/*.png", "static/*.ico"]' in pyproject


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


def test_gui_main_rejects_windows_system_directory(monkeypatch, capsys) -> None:
    from memoryguard import cli

    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    result = cli.gui_main([r"C:\Windows\System32"])

    assert result == 2
    assert "refusing to use a Windows system directory" in capsys.readouterr().err


def test_gui_workspace_prefers_environment(monkeypatch, tmp_path) -> None:
    from memoryguard.cli import _resolve_gui_workspace

    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    assert _resolve_gui_workspace([]) == tmp_path.resolve()
