"""Contract tests for the Phase 0 baseline gate."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "accept_v2_phase0.py"


def _run(*args: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(result.stdout)
    return result, payload


@pytest.fixture(scope="module")
def fixture_report() -> dict:
    result, payload = _run()
    assert result.returncode == 0, result.stderr
    return payload


def test_default_entry_and_all_phase0_gates(fixture_report: dict) -> None:
    assert fixture_report["schema"] == "memoryguard-v2-phase0"
    assert fixture_report["schema_version"] == 1
    assert fixture_report["mode"] == "fixture"
    assert fixture_report["ok"] is True
    assert all(fixture_report["gates"].values())
    assert not fixture_report["failures"]
    expected = {
        "exact_acl_deny",
        "same_text_distinct_occurrence",
        "stable_event_replay",
        "partial_no_delete",
        "complete_delete_recover",
        "over_10k_cursor",
        "evidence_pinned_revision",
        "no_op_zero_growth",
    }
    assert expected <= set(fixture_report["golden_queries"])
    assert fixture_report["source_inventory"]["scale"]["events"] > 10_000


def test_baseline_digest_is_deterministic() -> None:
    first_result, first = _run()
    second_result, second = _run()
    assert first_result.returncode == second_result.returncode == 0
    assert first["baseline_digest"] == second["baseline_digest"]
    assert first["golden_queries"] == second["golden_queries"]
    assert first["source_inventory"] == second["source_inventory"]


def test_explicit_empty_workspace_is_not_configured(tmp_path: Path) -> None:
    workspace = tmp_path / "empty-workspace"
    result, payload = _run("--workspace", str(workspace))
    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["source_inventory"]["status"] == "NOT_CONFIGURED"
    assert not workspace.exists(), "metadata probe must not create workspace"


def test_workspace_probe_never_outputs_content_or_writes(tmp_path: Path) -> None:
    workspace = tmp_path / "existing-workspace"
    workspace.mkdir()
    before = sorted(path.relative_to(workspace).as_posix() for path in workspace.rglob("*"))
    result, payload = _run("--workspace", str(workspace))
    after = sorted(path.relative_to(workspace).as_posix() for path in workspace.rglob("*"))
    assert result.returncode == 0
    assert payload["gates"]["workspace_read_only"] is True
    assert before == after == []
    serialized = result.stdout
    assert "phase0 evidence sample" not in serialized
    assert "bulk turn" not in serialized
    assert ".memoryguard" not in serialized or payload["source_inventory"]["layout"]["memoryguard_exists"] is False
