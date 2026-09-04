from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from memoryguard.usage_telemetry import record_conversion_event


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_usage_telemetry.py"
SPEC = importlib.util.spec_from_file_location("usage_telemetry_benchmark", SCRIPT)
assert SPEC and SPEC.loader
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def test_missing_database_is_fail_closed_and_does_not_create_it(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    report = benchmark.build_report(workspace, now_utc="2026-09-04T00:00:00Z")

    assert report["sample_status"] == "no_sample"
    assert report["sample_reason"] == "no_local_telemetry_database"
    assert report["estimated"]["state"] == "not_available"
    assert report["measured"]["state"] == "not_available"
    assert not (workspace / ".memoryguard").exists()


def test_report_keeps_estimated_savings_separate_from_measured_tokens(tmp_path: Path) -> None:
    record_conversion_event(
        tmp_path,
        provider="codex",
        program="codex",
        observed_at_utc="2026-09-04T01:00:00Z",
        baseline_units=100,
        delivered_units=40,
        event_id="benchmark-estimate",
    )

    report = benchmark.build_report(tmp_path, now_utc="2026-09-04T02:00:00Z")

    assert report["sample_status"] == "available"
    assert report["estimated"] == {
        "basis": "mg_deterministic_unit",
        "conversion_count": 1,
        "estimated_baseline_units": 100,
        "estimated_delivered_units": 40,
        "state": "estimated",
        "estimated_saved_units": 60,
        "estimated_increase_units": None,
        "estimated_ratio": 0.6,
    }
    assert report["measured"]["state"] == "not_available"
    assert report["measured"]["savings_claim"] == "unsupported"


def test_require_sample_returns_nonzero_for_no_sample(tmp_path: Path) -> None:
    assert benchmark.main([
        "--workspace", str(tmp_path / "workspace"), "--require-sample",
    ]) == 2


def test_unknown_measurement_basis_is_unsupported_and_not_available(tmp_path: Path) -> None:
    record_conversion_event(
        tmp_path,
        provider="codex",
        program="codex",
        observed_at_utc="2026-09-04T01:00:00Z",
        baseline_units=100,
        delivered_units=40,
        measurement_basis="other",
        event_id="benchmark-unknown-basis",
    )

    report = benchmark.build_report(tmp_path, now_utc="2026-09-04T02:00:00Z")

    assert report["sample_status"] == "unsupported"
    assert report["sample_reason"] == "unsupported_measurement_basis"
    assert report["estimated"]["state"] == "not_available"
    assert benchmark.main([
        "--workspace", str(tmp_path), "--require-sample",
        "--now-utc", "2026-09-04T02:00:00Z",
    ]) == 2


def test_report_public_json_omits_raw_scope_and_path_identifiers(tmp_path: Path) -> None:
    raw_share_group = r"shared-private\\agent"
    raw_project = r"C:\\Users\\private\\workspace"
    report = benchmark.build_report(
        tmp_path,
        share_group_id=raw_share_group,
        project_ref=raw_project,
        now_utc="2026-09-04T02:00:00Z",
    )
    encoded = json.dumps(report, ensure_ascii=False)

    assert raw_share_group not in encoded
    assert raw_project not in encoded
    assert "share_group_id" not in report
    assert "project_ref" not in report
    assert str(tmp_path) not in encoded


def test_measured_claim_names_provider_counts_and_derived_total(tmp_path: Path) -> None:
    # Inject a summary-shaped report through the existing helper's source DB.
    from memoryguard.usage_telemetry import _connect, _insert_event

    with _connect(tmp_path) as connection:
        _insert_event(connection, {
            "event_key": "measured-public-labels",
            "event_kind": "measured",
            "provider": "codex",
            "program": "codex",
            "agent_stable_key": "codex:codex",
            "source_kind": "fixture",
            "source_hash": "fixture",
            "source_generation": 0,
            "source_offset": 0,
            "source_ordinal": 0,
            "observed_at_utc": "2026-09-04T01:00:00.000Z",
            "measurement_basis": "provider_reported_token",
            "input_tokens": 11,
            "cached_input_tokens": None,
            "output_tokens": 7,
            "reasoning_output_tokens": None,
            "total_tokens": 18,
            "baseline_units": None,
            "delivered_units": None,
            "conversion_count": 0,
            "share_group_hash": None,
            "project_ref_hash": None,
            "scope_kind": "host",
        })
        connection.commit()

    measured = benchmark.build_report(tmp_path, now_utc="2026-09-04T02:00:00Z")["measured"]
    assert measured["provider_reported_input_tokens"] == 11
    assert measured["provider_reported_output_tokens"] == 7
    assert measured["provider_reported_total_tokens"] == 18
    assert measured["derived_total_tokens"] == 18
    assert measured["total_coverage"] == {
        "provider_reported": "complete",
        "input_output_derived": "complete",
        "measured_event_count": 1,
    }


def test_public_report_sanitizes_legacy_agent_status_fields(tmp_path: Path, monkeypatch) -> None:
    record_conversion_event(
        tmp_path,
        provider="codex",
        program="codex",
        observed_at_utc="2026-09-04T01:00:00Z",
        baseline_units=8,
        delivered_units=4,
        event_id="benchmark-legacy-status",
    )

    monkeypatch.setattr(
        benchmark,
        "get_usage_summary",
        lambda *_args, **_kwargs: {
            "window_days": 7,
            "generated_at_utc": "2026-09-04T02:00:00Z",
            "status": "available",
            "measurement_state": "estimated",
            "summary": {"estimated_baseline_units": 8, "estimated_delivered_units": 4, "conversion_count": 1},
            "agents": [{
                "provider": r"C:\\Users\\private\\account",
                "program": r"C:\\Users\\private\\token",
                "measurement_state": "estimated",
                "host_measurement_status": "error",
                "host_measurement_reason": r"RuntimeError: secret=C:\\private\\token",
                "estimated_baseline_units": 8,
                "estimated_delivered_units": 4,
                "conversion_count": 1,
            }],
        },
    )

    report = benchmark.build_report(tmp_path, now_utc="2026-09-04T02:00:00Z")
    encoded = json.dumps(report, ensure_ascii=False)
    assert r"C:\\Users\\private" not in encoded
    assert "RuntimeError" not in encoded
    assert "secret=" not in encoded
    assert report["agents"][0]["provider"] == "unknown"
    assert report["agents"][0]["program"] == "unknown"
    assert report["agents"][0]["host_measurement_reason"] == "sync_failed"
