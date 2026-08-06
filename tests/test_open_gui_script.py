"""Smoke tests for the source-checkout GUI launcher (scripts/open_gui.py).

The launcher must work from a bare checkout without installing the package:
it prepends the repo's ``src/`` to ``sys.path`` before importing.  The tests
load the script via importlib and drive its ``main()`` with a patched
``open_localhost_window`` so no browser is ever opened.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "open_gui.py"

sys.path.insert(0, str(PROJECT_ROOT / "src"))  # noqa: E402

from memoryguard import gui  # noqa: E402


def _load_script() -> "module":
    spec = importlib.util.spec_from_file_location("open_gui_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_open_gui_script_injects_src_and_delegates(tmp_path, monkeypatch) -> None:
    calls: dict = {}

    def fake_open_localhost_window(workspace, *, auto_open=True, **_kw):
        calls["workspace"] = str(Path(workspace).resolve())
        calls["auto_open"] = auto_open
        return 0, "http://127.0.0.1:0/"

    monkeypatch.setattr(gui, "open_localhost_window", fake_open_localhost_window)

    mod = _load_script()

    # The src-layout bootstrap ran, so a bare checkout can import the package.
    assert str((PROJECT_ROOT / "src").resolve()) in sys.path

    assert mod.main([str(tmp_path)]) == 0
    assert calls == {
        "workspace": str(tmp_path.resolve()),
        "auto_open": True,
    }


def test_open_gui_script_main_returns_rc_on_failure(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        gui, "open_localhost_window", lambda *a, **k: (3, "")
    )

    mod = _load_script()

    assert mod.main(["."]) == 3
    assert "Failed to start GUI (exit code 3)" in capsys.readouterr().err
