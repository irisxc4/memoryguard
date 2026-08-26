"""Editable/local-source MCP runtime origin and immutable snapshot launch."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from memoryguard.provider_adapters import (
    MCP_MODULE,
    MCP_UTF8_ARGS,
    _build_runtime_snapshot,
    _source_snapshot_key,
    _venv_python,
    prepare_provider_mcp_launch,
)
from memoryguard.runtime_lease import inspect_distribution_origin
from memoryguard.runtime_v2.safe_services import RuntimeDiagnosticsService


@pytest.fixture(autouse=True)
def _clear_runtime_python_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMORYGUARD_RUNTIME_PYTHON", raising=False)


def _direct_url(*, editable: bool = False, url: str = "file:///tmp/memoryguard", archive: bool = False) -> dict:
    payload: dict = {"url": url}
    if archive:
        payload["archive_info"] = {"hash": "sha256:abc"}
    else:
        payload["dir_info"] = {"editable": editable}
    return payload


def _src_package(tmp_path: Path) -> Path:
    src = tmp_path / "src" / "memoryguard"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='agent-memguard'\n", encoding="utf-8")
    return src / "__init__.py"


def _site_packages_package(tmp_path: Path) -> Path:
    package = tmp_path / "venv" / "Lib" / "site-packages" / "memoryguard" / "__init__.py"
    package.parent.mkdir(parents=True)
    package.write_text("", encoding="utf-8")
    return package


def _editable_origin() -> dict:
    return {
        "install_kind": "editable",
        "install_reason": "direct_url_editable",
        "editable": True,
        "source_drift_risk": True,
    }


def _write_source(root: Path, body: str = "[project]\nname='agent-memguard'\n") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(body, encoding="utf-8")
    return root


def _write_snapshot_python(snapshot_root: Path) -> Path:
    python = _venv_python(snapshot_root)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("ok", encoding="utf-8")
    return python


def _venv_from_argv(argv: list[str]) -> Path:
    venv_dir = Path(argv[-1])
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def test_inspect_direct_url_editable_is_machine_readable(tmp_path: Path) -> None:
    origin = inspect_distribution_origin(
        tmp_path,
        direct_url=_direct_url(editable=True),
        package_file=_src_package(tmp_path),
    )
    assert origin["install_kind"] == "editable"
    assert origin["install_reason"] == "direct_url_editable"
    assert origin["editable"] is True
    assert origin["source_drift_risk"] is True
    encoded = json.dumps(origin)
    assert "tmp" not in encoded
    assert "file:" not in encoded


def test_inspect_local_dir_and_pypi_wheel(tmp_path: Path) -> None:
    local = inspect_distribution_origin(
        tmp_path,
        direct_url=_direct_url(editable=False),
        package_file=_src_package(tmp_path),
    )
    assert local == {
        "install_kind": "local_source",
        "install_reason": "direct_url_local_path",
        "editable": False,
        "source_drift_risk": True,
    }
    pypi = inspect_distribution_origin(
        tmp_path,
        direct_url=_direct_url(
            url="https://files.pythonhosted.org/packages/agent_memguard-0.7.3-py3-none-any.whl",
            archive=True,
        ),
        package_file=tmp_path / "site-packages" / "memoryguard" / "__init__.py",
    )
    assert pypi["install_kind"] == "installed"
    assert pypi["install_reason"] == "distribution_installed"
    assert pypi["editable"] is False
    assert pypi["source_drift_risk"] is False


def test_inspect_file_url_site_packages_is_installed_no_drift(tmp_path: Path) -> None:
    origin = inspect_distribution_origin(
        tmp_path,
        direct_url=_direct_url(editable=False),
        package_file=_site_packages_package(tmp_path),
    )
    assert origin == {
        "install_kind": "installed",
        "install_reason": "distribution_installed",
        "editable": False,
        "source_drift_risk": False,
    }
    encoded = json.dumps(origin)
    assert "file:" not in encoded
    assert "site-packages" not in encoded
    assert "tmp" not in encoded


def test_inspect_file_url_source_checkout_is_local_source_drift(tmp_path: Path) -> None:
    origin = inspect_distribution_origin(
        tmp_path,
        direct_url=_direct_url(editable=False),
        package_file=_src_package(tmp_path),
    )
    assert origin == {
        "install_kind": "local_source",
        "install_reason": "direct_url_local_path",
        "editable": False,
        "source_drift_risk": True,
    }
    encoded = json.dumps(origin)
    assert "file:" not in encoded
    assert "tmp" not in encoded


def test_inspect_source_tree_without_direct_url(tmp_path: Path) -> None:
    origin = inspect_distribution_origin(
        tmp_path,
        direct_url="",
        package_file=_src_package(tmp_path),
    )
    assert origin["install_kind"] == "local_source"
    assert origin["install_reason"] == "source_tree_on_sys_path"


def test_live_inspect_never_leaks_paths_or_urls() -> None:
    origin = inspect_distribution_origin()
    encoded = json.dumps(origin)
    assert origin["install_kind"] in {"editable", "local_source", "installed", "unknown"}
    assert origin["install_reason"] in {
        "direct_url_editable",
        "direct_url_local_path",
        "source_tree_on_sys_path",
        "distribution_installed",
        "metadata_unavailable",
    }
    assert "file:" not in encoded
    assert "http" not in encoded
    assert ":\\" not in encoded
    assert "H:/" not in encoded


def test_inspect_missing_metadata_is_unknown(tmp_path: Path) -> None:
    origin = inspect_distribution_origin(tmp_path, direct_url="", package_file=None)
    assert origin["install_kind"] == "unknown"
    assert origin["install_reason"] == "metadata_unavailable"


def test_runtime_diagnostics_reports_editable_reason_without_mutating(tmp_path: Path) -> None:
    calls: list[str] = []

    def origin_provider(_workspace):
        calls.append("origin")
        return {
            "install_kind": "editable",
            "install_reason": "direct_url_editable",
            "editable": True,
            "source_drift_risk": True,
        }

    def status_provider(_workspace):
        return {
            "state": "V2_ACTIVE",
            "split_brain": True,
            "restart_required": True,
            "live": [{"pid": 11, "memoryguard_version": "0.7.3", "code_fingerprint": "abc"}],
            "stale": [],
            "conflicting": [{"pid": 11, "memoryguard_version": "0.7.3", "code_fingerprint": "abc"}],
            "command": "C:/secret/python.exe -m memoryguard.mcp_server",
        }

    before = list(tmp_path.iterdir())
    result = RuntimeDiagnosticsService(
        tmp_path,
        version_provider=lambda: "0.7.3",
        status_provider=status_provider,
        origin_provider=origin_provider,
    ).memoryguard_runtime_processes({}, context={"is_admin": True})
    assert result["status"] == "READY"
    summary = result["summary"]
    assert summary["install_kind"] == "editable"
    assert summary["install_reason"] == "direct_url_editable"
    assert summary["editable_install"] is True
    assert summary["source_drift_risk"] is True
    assert summary["split_brain"] is True
    assert summary["split_brain_reason"] == "editable_source_fingerprint_drift"
    assert summary["live_processes"] == 1
    assert summary["conflicts"] == 1
    encoded = json.dumps(result)
    assert "secret" not in encoded
    assert "C:/" not in encoded
    assert list(tmp_path.iterdir()) == before
    assert calls == ["origin"]


def test_diagnostics_do_not_select_or_build_snapshot(tmp_path: Path) -> None:
    def boom(**_kwargs):
        raise AssertionError("diagnostics must not build a snapshot")

    source = _write_source(tmp_path / "repo")
    leftover = _write_snapshot_python(tmp_path / "mcp-runtime" / _source_snapshot_key(source))
    before = leftover.read_text(encoding="utf-8")
    launch = prepare_provider_mcp_launch(
        mutate=False,
        origin=_editable_origin(),
        snapshot_root=tmp_path / "mcp-runtime",
        source_root=source,
        builder=boom,
    )
    assert launch["ok"] is True
    assert launch["mutated"] is False
    assert launch["snapshot"] is False
    assert launch["reason"] == "editable_install_snapshot_required"
    assert leftover.read_text(encoding="utf-8") == before
    assert list((tmp_path / "mcp-runtime").glob(".mg-snap-*")) == []


def test_installed_origin_keeps_portable_interpreter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.delenv("MEMORYGUARD_RUNTIME_PYTHON", raising=False)
    leftover = _write_snapshot_python(tmp_path / "mcp-runtime")
    origin = {
        "install_kind": "installed",
        "install_reason": "distribution_installed",
        "editable": False,
        "source_drift_risk": False,
    }
    launch = prepare_provider_mcp_launch(
        mutate=True,
        origin=origin,
        snapshot_root=tmp_path / "mcp-runtime",
        builder=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("pypi install must not snapshot")),
    )
    assert launch["ok"] is True
    assert launch["python"] == sys.executable
    assert launch["python"] != str(leftover)
    assert launch["argv"] == [sys.executable, "-X", "utf8", "-m", MCP_MODULE]
    assert launch["argv"][1:] == MCP_UTF8_ARGS
    assert launch["snapshot"] is False
    assert launch["mutated"] is False


def test_injected_copied_origin_keeps_current_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    monkeypatch.delenv("MEMORYGUARD_RUNTIME_PYTHON", raising=False)
    leftover = _write_snapshot_python(tmp_path / "mcp-runtime")
    origin = inspect_distribution_origin(
        tmp_path,
        direct_url=_direct_url(editable=False),
        package_file=_site_packages_package(tmp_path),
    )
    launch = prepare_provider_mcp_launch(
        mutate=True,
        origin=origin,
        snapshot_root=tmp_path / "mcp-runtime",
        builder=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("copied local install must not snapshot")
        ),
    )
    assert origin["install_kind"] == "installed"
    assert origin["editable"] is False
    assert origin["source_drift_risk"] is False
    assert launch["ok"] is True
    assert launch["python"] == sys.executable
    assert launch["python"] != str(leftover)
    assert launch["argv"] == [sys.executable, "-X", "utf8", "-m", MCP_MODULE]
    assert launch["snapshot"] is False
    assert launch["mutated"] is False


def test_editable_install_uses_explicit_snapshot_python(tmp_path: Path) -> None:
    fake_python = tmp_path / "snapshot" / "python.exe"
    fake_python.parent.mkdir()
    fake_python.write_text("", encoding="utf-8")
    launch = prepare_provider_mcp_launch(
        mutate=True,
        origin=_editable_origin(),
        snapshot_python=str(fake_python),
        builder=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("existing snapshot must be selected")),
    )
    assert launch["ok"] is True
    assert launch["snapshot"] is True
    assert launch["python"] == str(fake_python)
    assert launch["argv"][0] == str(fake_python)
    assert launch["argv"][1:] == ["-X", "utf8", "-m", MCP_MODULE]


def test_runtime_python_override_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "override" / "python.exe"
    override.parent.mkdir()
    override.write_text("override", encoding="utf-8")
    monkeypatch.setenv("MEMORYGUARD_RUNTIME_PYTHON", str(override))
    source = _write_source(tmp_path / "repo", "[project]\nname='agent-memguard'\nversion='changed'\n")
    snapshot_root = tmp_path / "mcp-runtime"
    _write_snapshot_python(snapshot_root / "stale")

    def boom(**_kwargs):
        raise AssertionError("override must not build or refresh a snapshot")

    launch = prepare_provider_mcp_launch(
        mutate=True,
        origin=_editable_origin(),
        snapshot_root=snapshot_root,
        source_root=source,
        builder=boom,
    )
    assert launch["ok"] is True
    assert launch["python"] == str(override)
    assert launch["argv"][0] == str(override)
    assert launch["mutated"] is False
    assert launch["snapshot"] is True


def test_editable_mutate_builds_non_editable_snapshot(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "repo")
    snapshot_root = tmp_path / "mcp-runtime"
    commands: list[list[str]] = []

    def runner(argv: list[str]) -> None:
        commands.append(list(argv))
        if argv[1:3] == ["-m", "venv"]:
            python = _venv_from_argv(argv)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")

    python = _build_runtime_snapshot(
        snapshot_root=snapshot_root,
        source_root=source,
        runner=runner,
    )
    assert any(item[1:3] == ["-m", "venv"] for item in commands)
    pip_cmds = [item for item in commands if "pip" in item]
    assert pip_cmds
    assert "--no-deps" in pip_cmds[-1]
    assert "--upgrade" in pip_cmds[-1]
    assert str(source) in pip_cmds[-1]
    assert "-e" not in pip_cmds[-1]
    assert "--editable" not in pip_cmds[-1]
    marker = json.loads((snapshot_root / "origin.json").read_text(encoding="utf-8"))
    assert marker["editable"] is False
    assert marker["source_key"] == _source_snapshot_key(source)
    assert Path(python).name.startswith("python")
    assert Path(python).is_file()
    assert list(snapshot_root.parent.glob(".mg-snap-*")) == []

    built: list[tuple] = []

    def builder(*, snapshot_root, source_root):
        built.append((Path(snapshot_root), Path(source_root)))
        fake = _write_snapshot_python(Path(snapshot_root))
        return str(fake)

    parent = snapshot_root / "fresh"
    launch = prepare_provider_mcp_launch(
        mutate=True,
        origin=_editable_origin(),
        snapshot_root=parent,
        source_root=source,
        builder=builder,
    )
    assert built == [(parent / _source_snapshot_key(source), source)]
    assert launch["ok"] is True
    assert launch["mutated"] is True
    assert launch["snapshot"] is True
    assert launch["argv"][1:] == ["-X", "utf8", "-m", MCP_MODULE]


def test_unchanged_source_reuses_snapshot(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "repo")
    snapshot_root = tmp_path / "mcp-runtime"
    calls: list[Path] = []

    def builder(*, snapshot_root, source_root):
        del source_root
        dest = Path(snapshot_root)
        calls.append(dest)
        return str(_write_snapshot_python(dest))

    first = prepare_provider_mcp_launch(
        mutate=True,
        origin=_editable_origin(),
        snapshot_root=snapshot_root,
        source_root=source,
        builder=builder,
    )
    second = prepare_provider_mcp_launch(
        mutate=True,
        origin=_editable_origin(),
        snapshot_root=snapshot_root,
        source_root=source,
        builder=builder,
    )
    assert first["ok"] is True
    assert second["ok"] is True
    assert len(calls) == 1
    assert first["python"] == second["python"]
    assert Path(second["python"]).is_file()
    assert second["mutated"] is False
    assert second["snapshot"] is True
    assert second["reason"] == "runtime_snapshot"


def test_changed_source_selects_fresh_snapshot(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "repo", "[project]\nname='agent-memguard'\nversion='1'\n")
    snapshot_root = tmp_path / "mcp-runtime"
    calls: list[Path] = []

    def builder(*, snapshot_root, source_root):
        del source_root
        dest = Path(snapshot_root)
        calls.append(dest)
        python = _write_snapshot_python(dest)
        python.write_text(dest.name, encoding="utf-8")
        return str(python)

    first = prepare_provider_mcp_launch(
        mutate=True,
        origin=_editable_origin(),
        snapshot_root=snapshot_root,
        source_root=source,
        builder=builder,
    )
    _write_source(source, "[project]\nname='agent-memguard'\nversion='2'\n")
    second = prepare_provider_mcp_launch(
        mutate=True,
        origin=_editable_origin(),
        snapshot_root=snapshot_root,
        source_root=source,
        builder=builder,
    )
    assert first["ok"] is True
    assert second["ok"] is True
    assert len(calls) == 2
    assert calls[0] != calls[1]
    assert first["python"] != second["python"]
    assert Path(first["python"]).is_file()
    assert Path(second["python"]).is_file()
    assert Path(first["python"]).read_text(encoding="utf-8") == calls[0].name
    assert second["mutated"] is True


def test_package_static_asset_selects_fresh_snapshot(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "repo")
    package = source / "src" / "memoryguard"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    static = package / "static"
    static.mkdir()
    asset = static / "cytoscape.min.js"
    asset.write_bytes(b"js-v1")
    notice = package / "graphify_core" / "NOTICE.md"
    notice.parent.mkdir()
    notice.write_text("notice-v1\n", encoding="utf-8")
    snapshot_root = tmp_path / "mcp-runtime"
    calls: list[Path] = []

    def builder(*, snapshot_root, source_root):
        del source_root
        dest = Path(snapshot_root)
        calls.append(dest)
        python = _write_snapshot_python(dest)
        python.write_text(dest.name, encoding="utf-8")
        return str(python)

    first_key = _source_snapshot_key(source)
    first = prepare_provider_mcp_launch(
        mutate=True,
        origin=_editable_origin(),
        snapshot_root=snapshot_root,
        source_root=source,
        builder=builder,
    )
    asset.write_bytes(b"js-v2")
    second_key = _source_snapshot_key(source)
    assert first_key != second_key
    second = prepare_provider_mcp_launch(
        mutate=True,
        origin=_editable_origin(),
        snapshot_root=snapshot_root,
        source_root=source,
        builder=builder,
    )
    assert first["ok"] is True
    assert second["ok"] is True
    assert len(calls) == 2
    assert calls[0] != calls[1]
    assert first["python"] != second["python"]
    assert second["mutated"] is True
    assert Path(second["python"]).read_text(encoding="utf-8") == calls[1].name

    pycache = package / "__pycache__"
    pycache.mkdir()
    (pycache / "gui.cpython-314.pyc").write_bytes(b"bytecode")
    (package / "gui.pyc").write_bytes(b"loose-pyc")
    pytest_cache = package / ".pytest_cache"
    pytest_cache.mkdir()
    (pytest_cache / "v").write_text("cache", encoding="utf-8")
    graphify_cache = package / "graphify-out" / "cache"
    graphify_cache.mkdir(parents=True)
    (graphify_cache / "stat-index.json").write_text("{}", encoding="utf-8")
    assert _source_snapshot_key(source) == second_key
    third = prepare_provider_mcp_launch(
        mutate=True,
        origin=_editable_origin(),
        snapshot_root=snapshot_root,
        source_root=source,
        builder=builder,
    )
    assert len(calls) == 2
    assert third["ok"] is True
    assert third["python"] == second["python"]
    assert third["mutated"] is False
    assert third["reason"] == "runtime_snapshot"


def test_builder_failure_preserves_old_selection(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "repo", "[project]\nname='agent-memguard'\nversion='1'\n")
    snapshot_root = tmp_path / "mcp-runtime"
    old_python = _write_snapshot_python(snapshot_root / _source_snapshot_key(source))
    old_python.write_text("old-good", encoding="utf-8")
    _write_source(source, "[project]\nname='agent-memguard'\nversion='2'\n")
    new_key = _source_snapshot_key(source)
    broken = snapshot_root / new_key

    def builder(*, snapshot_root, source_root):
        del snapshot_root, source_root
        raise RuntimeError("editable_install_snapshot_failed")

    launch = prepare_provider_mcp_launch(
        mutate=True,
        origin=_editable_origin(),
        snapshot_root=snapshot_root,
        source_root=source,
        builder=builder,
    )
    assert launch["ok"] is False
    assert launch["mutated"] is False
    assert launch["snapshot"] is False
    assert launch["reason"] == "editable_install_snapshot_failed"
    assert launch["argv"][0] != str(_venv_python(broken))
    assert launch["python"] != str(_venv_python(broken))
    assert not broken.exists()
    assert old_python.is_file()
    assert old_python.read_text(encoding="utf-8") == "old-good"
    assert list(snapshot_root.glob(".mg-snap-*")) == []


def test_build_failure_does_not_publish_dest(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "repo")
    dest = tmp_path / "mcp-runtime" / "dest"

    def runner(argv: list[str]) -> None:
        if argv[1:3] == ["-m", "venv"]:
            python = _venv_from_argv(argv)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("staging", encoding="utf-8")
            return
        raise RuntimeError("editable_install_snapshot_failed")

    with pytest.raises(RuntimeError, match="editable_install_snapshot_failed"):
        _build_runtime_snapshot(
            snapshot_root=dest,
            source_root=source,
            runner=runner,
        )
    assert not dest.exists()
    assert list((tmp_path / "mcp-runtime").glob(".mg-snap-*")) == []


def test_codex_install_writes_snapshot_interpreter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from memoryguard import toml_compat as tomllib
    from memoryguard.provider_adapters import CodexAdapter
    from memoryguard.runtime_v2.group_native import GroupControlService

    monkeypatch.setattr(
        "memoryguard.provider_adapters._binding_plane_for_workspace",
        lambda _workspace: "v2",
    )
    monkeypatch.setattr(
        "memoryguard.provider_adapters.inspect_distribution_origin",
        lambda *args, **kwargs: {
            "install_kind": "editable",
            "install_reason": "direct_url_editable",
            "editable": True,
            "source_drift_risk": True,
        },
    )
    fake_python = tmp_path / "snap" / "python.exe"
    fake_python.parent.mkdir()
    fake_python.write_text("", encoding="utf-8")
    source = tmp_path / "repo"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='agent-memguard'\n", encoding="utf-8")
    monkeypatch.setattr(
        "memoryguard.provider_adapters._build_runtime_snapshot",
        lambda **_kwargs: str(fake_python),
    )
    monkeypatch.setattr(
        "memoryguard.provider_adapters._live_source_root",
        lambda: source,
    )
    monkeypatch.setattr(
        "memoryguard.provider_adapters._in_repository_tests",
        lambda: False,
    )
    monkeypatch.setenv("MEMORYGUARD_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    GroupControlService(workspace, write=True).bind_agent("codex-snap", "snap-group")
    adapter = CodexAdapter(workspace)
    adapter.install(workspace, share_group_id="snap-group", agent_instance_id="codex-snap")
    parsed = tomllib.loads((workspace / ".codex" / "config.toml").read_text(encoding="utf-8"))
    server = parsed["mcp_servers"]["memoryguard"]
    assert server["command"] == str(fake_python)
    assert server["args"] == ["-X", "utf8", "-m", "memoryguard.mcp_server"]
