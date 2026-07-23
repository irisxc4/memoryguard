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
    if graph.get("empty"):
        assert "reason" in graph
    else:
        assert "stats" in graph
        assert "nodes" in graph


def test_pyproject_declares_windowed_gui_entry() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "[project.gui-scripts]" in pyproject
    assert 'memoryguard-gui = "memoryguard.cli:gui_main"' in pyproject
    assert (PROJECT_ROOT / "MemoryGuard.pyw").exists()


def test_gui_main_falls_back_to_static_html_without_localhost(monkeypatch, tmp_path) -> None:
    from memoryguard import cli
    from memoryguard import gui

    opened = []
    monkeypatch.setattr(gui, "has_native_gui", lambda: False)
    monkeypatch.setattr(cli, "run_audit", lambda workspace: {"workspace": str(workspace)})
    monkeypatch.setattr(cli, "render_html_report", lambda report: "<html>MemoryGuard</html>")
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url) or True)

    result = cli.gui_main([str(tmp_path)])

    assert result == 0
    assert opened == [(tmp_path / ".memoryguard" / "reports" / "report.html").resolve().as_uri()]
    assert (tmp_path / ".memoryguard" / "reports" / "report.html").exists()
