from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

from memoryguard.migration.ready_prepare import prepare_v2_ready
from memoryguard.system.manifest import ManifestManager, ManifestState
from memoryguard.v2_cli import main as v2_cli_main


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepare_v2_ready.py"


def test_prepare_v2_ready_reaches_ready_and_never_activates(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    result = prepare_v2_ready(
        workspace,
        apply=True,
        data_home=tmp_path / "data",
    )
    assert result["status"] == "V2_READY", result
    assert result["ok"] is True
    assert result["ready"] is True
    assert result["v2_active"] is False
    assert result["activation_required"] is True
    record = ManifestManager(workspace).current()
    assert record.state is ManifestState.V2_READY
    assert record.errors["readiness_source_verification"]["status"] == "PASS"


def test_prepare_v2_ready_dry_run_reports_live_wal_without_writing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    db = workspace / ".memoryguard" / "shared-memory" / "g" / "memory.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE records(memory_id TEXT PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO records VALUES('m','body')")
        conn.commit()
        assert Path(str(db) + "-wal").is_file()
        result = prepare_v2_ready(workspace, apply=False, data_home=tmp_path / "data")
        assert result["status"] == "DRY_RUN_REQUIRES_SNAPSHOT"
        assert result["ok"] is True
        assert result["reason"] == "live_wal_requires_online_snapshot"
        assert result["v2_active"] is False
        assert ManifestManager(workspace).current(immutable=True).state is ManifestState.V1_ACTIVE
    finally:
        conn.close()


def test_packaged_v2_operator_cli_status_is_zero_write(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    assert v2_cli_main(["status", "-w", str(workspace)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "V1_ACTIVE"
    assert payload["v2_active"] is False
    assert not workspace.exists()


def test_packaged_v2_operator_cli_requires_explicit_activation_confirmation(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    data_home = tmp_path / "data"

    assert v2_cli_main([
        "prepare", "-w", str(workspace), "--data-home", str(data_home), "--apply",
    ]) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["status"] == "V2_READY"
    assert prepared["v2_active"] is False
    ready = ManifestManager(workspace).current()
    assert ready.state is ManifestState.V2_READY

    assert v2_cli_main([
        "activate", "-w", str(workspace), "--confirm", "yes",
    ]) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["code"] == "activation_confirmation_mismatch"
    assert ManifestManager(workspace).current().state is ManifestState.V2_READY

    assert v2_cli_main([
        "activate", "-w", str(workspace), "--confirm", "V2_ACTIVE",
        "--expected-generation", str(ready.generation),
    ]) == 0
    active = json.loads(capsys.readouterr().out)
    assert active["status"] == "V2_ACTIVE"
    assert active["v2_active"] is True
    assert ManifestManager(workspace).current().state is ManifestState.V2_ACTIVE


def test_prepare_v2_ready_script_defaults_to_zero_write(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    data_home = tmp_path / "data-home"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--workspace",
            str(workspace),
            "--data-home",
            str(data_home),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert report["status"] == "DRY_RUN"
    assert report["v2_active"] is False
    assert not workspace.exists()
    assert not data_home.exists()
