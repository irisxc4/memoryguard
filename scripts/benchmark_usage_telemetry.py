"""Emit a fail-closed local MemoryGuard token-evidence report as JSON.

Default mode is read-only: a missing telemetry database returns ``no_sample``
without creating one.  ``--sync`` explicitly imports available host reports
and may update the workspace-local telemetry database.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memoryguard.usage_telemetry import (  # noqa: E402
    DETERMINISTIC_BASIS,
    _PUBLIC_MEASUREMENT_STATES,
    _safe_agent_name,
    _safe_sync_reason,
    _safe_sync_status,
    get_usage_summary,
    sync_usage_telemetry,
    telemetry_db_path,
)


REPORT_SCHEMA_VERSION = 1
_USABLE_CLAIM_STATES = frozenset({"measured", "estimated"})
_PUBLIC_SYNC_STATUS = frozenset({
    "not_requested", "success", "source_not_found", "host_not_supported",
    "no_measured_source", "unavailable", "error",
})
_PUBLIC_COVERAGE = frozenset({"complete", "partial", "none"})
_CLAIM_BOUNDARY = {
    "measured": (
        "Provider-reported input/output token counts only. Total is derived "
        "from those counts; no billing, cost, or savings claim."
    ),
    "estimated": (
        "MemoryGuard deterministic baseline/delivered units only; savings "
        "ratio is estimated, not provider-reported or billing tokens."
    ),
    "unsupported": (
        "Unknown or unsupported measurement basis is excluded; it is not "
        "reported as zero usage or zero savings."
    ),
}


def _int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _estimated_claim(metrics: Mapping[str, Any]) -> dict[str, Any]:
    baseline = _int(metrics.get("estimated_baseline_units"))
    delivered = _int(metrics.get("estimated_delivered_units"))
    conversions = _int(metrics.get("conversion_count")) or 0
    claim: dict[str, Any] = {
        "basis": DETERMINISTIC_BASIS,
        "conversion_count": conversions,
        "estimated_baseline_units": baseline,
        "estimated_delivered_units": delivered,
        "state": "not_available",
        "estimated_saved_units": None,
        "estimated_increase_units": None,
        "estimated_ratio": None,
    }
    if baseline is None or delivered is None or baseline <= 0:
        return claim
    delta = baseline - delivered
    if delta < 0:
        claim.update(state="increase", estimated_increase_units=-delta)
        return claim
    claim.update(
        state="estimated",
        estimated_saved_units=delta,
        estimated_ratio=delta / baseline,
    )
    return claim


def _measured_claim(metrics: Mapping[str, Any]) -> dict[str, Any]:
    events = _int(metrics.get("measured_event_count")) or 0
    provider_input = _int(metrics.get("measured_input"))
    provider_output = _int(metrics.get("measured_output"))
    derived_total = _int(metrics.get("measured_derived_total"))
    coverage = metrics.get("measured_total_coverage")
    if not isinstance(coverage, Mapping):
        coverage = {
            "provider_reported": "complete" if events and _int(metrics.get("measured_total")) is not None else "none",
            "input_output_derived": "complete" if derived_total is not None else "none",
            "measured_event_count": events,
        }
    public_coverage = {
        "provider_reported": (
            str(coverage.get("provider_reported"))
            if str(coverage.get("provider_reported")) in _PUBLIC_COVERAGE
            else "none"
        ),
        "input_output_derived": (
            str(coverage.get("input_output_derived"))
            if str(coverage.get("input_output_derived")) in _PUBLIC_COVERAGE
            else "none"
        ),
        "measured_event_count": _int(coverage.get("measured_event_count")) or events,
    }
    provider_total = _int(metrics.get("measured_total"))
    return {
        "basis": "provider_reported_token",
        "state": "measured" if events else "not_available",
        "measured_event_count": events,
        "provider_reported_input_tokens": provider_input,
        "provider_reported_output_tokens": provider_output,
        "provider_reported_total_tokens": provider_total,
        "derived_total_tokens": derived_total,
        "provider_total_event_count": _int(metrics.get("measured_provider_total_event_count")) or 0,
        "derived_total_event_count": _int(metrics.get("measured_derived_total_event_count")) or 0,
        "total_coverage": public_coverage,
        # Keep existing names for GUI/report consumers during schema v1.
        "measured_input_tokens": provider_input,
        "measured_output_tokens": provider_output,
        "measured_total_tokens": provider_total,
        "savings_claim": "unsupported",
    }


def _public_sync(sync: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist sync counters; never expose adapter paths or identifiers."""

    public: dict[str, Any] = {"requested": bool(sync.get("requested"))}
    status = str(sync.get("status") or "unavailable")
    public["status"] = status if status in _PUBLIC_SYNC_STATUS else "unavailable"
    for key in ("inserted", "rotated", "sources", "codex_inserted", "grok_inserted"):
        value = _int(sync.get(key))
        if value is not None:
            public[key] = value
    return public


def _agents(summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "provider": _safe_agent_name(item.get("provider")),
            "program": _safe_agent_name(item.get("program")),
            "measurement_state": (
                str(item.get("measurement_state"))
                if str(item.get("measurement_state")) in _PUBLIC_MEASUREMENT_STATES
                else "unavailable"
            ),
            "host_measurement_status": _safe_sync_status(
                item.get("host_measurement_status") or "not_synced"
            ),
            "host_measurement_reason": _safe_sync_reason(
                item.get("host_measurement_reason"),
                default=(
                    "sync_failed"
                    if _safe_sync_status(item.get("host_measurement_status")) == "error"
                    else "not_synced"
                ),
            ),
            "measured": _measured_claim(item),
            "estimated": _estimated_claim(item),
        }
        for item in summary.get("agents", [])
        if isinstance(item, Mapping)
    ]


def _report_time(value: str | datetime | None) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if value:
        return str(value)
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _empty_report(
    *, window_days: int, sample_reason: str, sync: Mapping[str, Any], now_utc: str | datetime | None,
) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "window_days": window_days,
        "generated_at_utc": _report_time(now_utc),
        "sample_status": "no_sample",
        "sample_reason": sample_reason,
        "sync": _public_sync(sync),
        "measured": _measured_claim({}),
        "estimated": _estimated_claim({}),
        "unsupported_host_measurements": [],
        "agents": [],
        "claim_boundary": dict(_CLAIM_BOUNDARY),
    }


def build_report(
    workspace: str | Path,
    *,
    window_days: int = 7,
    sync: bool = False,
    codex_home: str | Path | None = None,
    grok_home: str | Path | None = None,
    share_group_id: str = "",
    project_ref: str = "",
    agent_key: str | None = None,
    now_utc: str | datetime | None = None,
) -> dict[str, Any]:
    """Build report without inventing a sample, saving, or host measurement."""

    workspace_path = Path(workspace).expanduser().resolve()
    database = telemetry_db_path(workspace_path)
    sync_result: Mapping[str, Any] = {"requested": sync, "status": "not_requested"}
    if sync:
        sync_result = {"requested": True, **sync_usage_telemetry(
            workspace_path,
            codex_home=codex_home,
            grok_home=grok_home,
            now_utc=now_utc,
        )}
    if not database.is_file():
        return _empty_report(
            window_days=window_days,
            sample_reason="no_local_telemetry_database",
            sync=sync_result,
            now_utc=now_utc,
        )

    summary = get_usage_summary(
        workspace_path,
        window_days=window_days,
        now_utc=now_utc,
        share_group_id=share_group_id,
        project_ref=project_ref,
        agent_key=agent_key,
    )
    metrics = summary.get("summary") if isinstance(summary.get("summary"), Mapping) else {}
    agents = _agents(summary)
    unsupported = [
        {
            "provider": item["provider"],
            "program": item["program"],
            "reason": item["host_measurement_reason"],
        }
        for item in agents
        if item["host_measurement_status"] == "host_not_supported"
    ]
    measured = _measured_claim(metrics)
    estimated = _estimated_claim(metrics)
    has_usable_claim = (
        measured["state"] in _USABLE_CLAIM_STATES
        or estimated["state"] in _USABLE_CLAIM_STATES
    )
    summary_has_rows = summary.get("status") == "available"
    if has_usable_claim:
        sample_status = "available"
        sample_reason = None
    elif summary_has_rows:
        sample_status = "unsupported"
        sample_reason = "unsupported_measurement_basis"
    else:
        sample_status = "no_sample"
        sample_reason = summary.get("empty_reason", "no_events")
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "window_days": summary.get("window_days", window_days),
        "generated_at_utc": summary.get("generated_at_utc"),
        "sample_status": sample_status,
        "sample_reason": sample_reason,
        "sync": _public_sync(sync_result),
        "measurement_state": summary.get("measurement_state", "unavailable"),
        "measured": measured,
        "estimated": estimated,
        "unsupported_host_measurements": unsupported,
        "agents": agents,
        "claim_boundary": dict(_CLAIM_BOUNDARY),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--window-days", type=int, choices=(7, 30), default=7)
    parser.add_argument("--sync", action="store_true", help="Import host-reported events before reading.")
    parser.add_argument("--codex-home")
    parser.add_argument("--grok-home")
    parser.add_argument("--share-group-id", default="")
    parser.add_argument("--project-ref", default="")
    parser.add_argument("--agent-key")
    parser.add_argument("--now-utc", help="UTC ISO-8601 anchor; useful for reproducible tests.")
    parser.add_argument("--output", help="Optional JSON report path.")
    parser.add_argument("--require-sample", action="store_true", help="Exit 2 when no local sample exists.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = build_report(
            args.workspace,
            window_days=args.window_days,
            sync=args.sync,
            codex_home=args.codex_home,
            grok_home=args.grok_home,
            share_group_id=args.share_group_id,
            project_ref=args.project_ref,
            agent_key=args.agent_key,
            now_utc=args.now_utc,
        )
    except Exception as exc:
        report = {"schema_version": REPORT_SCHEMA_VERSION, "sample_status": "error", "error": type(exc).__name__}
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    if report.get("sample_status") == "error":
        return 1
    return 2 if args.require_sample and report.get("sample_status") != "available" else 0


if __name__ == "__main__":
    raise SystemExit(main())
