from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import memoryguard.usage_telemetry as usage_telemetry
from memoryguard.access_context import AccessContext
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.usage_telemetry import record_conversion_event


def _freeze_usage_clock(monkeypatch) -> None:
    monkeypatch.setattr(
        usage_telemetry,
        "_utc_now",
        lambda: usage_telemetry._parse_utc("2026-08-28T02:00:00Z"),
    )


def _context(workspace: Path, *, group: str = "group-a"):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="codex-agent",
            is_admin=False,
            strict_binding=True,
            allow_anon=False,
            session_id="usage-telemetry-test",
            session_source="test",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id=group,
        project_ref=str(workspace.resolve()),
        provider="codex",
        runtime_role="root",
    )


def _record(workspace: Path, provider: str, baseline: int, delivered: int, event_id: str) -> None:
    record_conversion_event(
        workspace,
        provider=provider,
        program=provider,
        share_group_id="group-a",
        project_ref=str(workspace.resolve()),
        observed_at_utc="2026-08-28T01:00:00Z",
        baseline_units=baseline,
        delivered_units=delivered,
        event_id=event_id,
    )


def test_gui_usage_telemetry_is_scoped_and_agent_filter_only_filters_results(
    tmp_path: Path, monkeypatch
) -> None:
    _freeze_usage_clock(monkeypatch)
    _record(tmp_path, "codex", 100, 40, "codex-1")
    _record(tmp_path, "grok", 80, 20, "grok-1")
    port = NativeV2RuntimePort(tmp_path)
    context = _context(tmp_path)

    all_agents = port.dispatch_gui(
        "get_usage_telemetry",
        [7, ""],
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )

    assert all_agents["ok"] is True, all_agents
    data = all_agents["data"]
    assert data["window_days"] == 7
    assert data["share_group_id"] == "group-a"
    assert {row["agent_key"] for row in data["rows"]} == {"codex:codex", "grok:grok"}

    codex = port.dispatch_gui(
        "get_usage_telemetry",
        [30, "codex:codex"],
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert codex["ok"] is True, codex
    assert codex["data"]["window_days"] == 30
    assert codex["data"]["summary"]["estimated_baseline_units"] == 100
    assert sum(
        item["estimated_baseline_units"] or 0
        for item in codex["data"]["series"]
    ) == 100
    assert {row["agent_key"] for row in codex["data"]["rows"]} == {"codex:codex"}
    assert {agent["agent_key"] for agent in codex["data"]["agents"]} == {"codex:codex"}


def test_gui_usage_telemetry_rejects_scope_spoof_and_invalid_window(tmp_path: Path) -> None:
    port = NativeV2RuntimePort(tmp_path)
    context = _context(tmp_path)

    spoofed = port.dispatch_gui(
        "get_usage_telemetry",
        {"window_days": 7, "agent_key": "grok:grok", "share_group_id": "other-group"},
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert spoofed["ok"] is False
    assert spoofed["code"] == "context_identity_spoof"

    invalid = port.dispatch_gui(
        "get_usage_telemetry",
        [14, ""],
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert invalid["ok"] is False
    assert invalid["code"] == "invalid_usage_window"


def test_gui_usage_sync_is_mutation_gated_and_returns_safe_summary(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[Path] = []

    def fake_sync(workspace, **_kwargs):
        calls.append(Path(workspace))
        return {
            "status": "error",
            "inserted": 2,
            "rotated": 1,
            "sources": 3,
            "sync_state": {
                "providers": {
                    "codex": {
                        "status": "success",
                        "inserted_count": 2,
                        "rotated_count": 1,
                        "source_count": 1,
                        "last_error": r"C:\\private\\source.jsonl",
                    },
                    "grok": {
                        "status": "error",
                        "inserted_count": 0,
                        "rotated_count": 0,
                        "source_count": 2,
                        "last_error": "adapter_failed",
                    },
                }
            },
        }

    monkeypatch.setattr(usage_telemetry, "sync_usage_telemetry", fake_sync)
    context = _context(tmp_path)

    class Manifest:
        def __init__(self, state: str) -> None:
            self.state = state

        def current(self) -> dict[str, object]:
            return {"state": self.state, "generation": 1}

    port = NativeV2RuntimePort(tmp_path, state_provider=Manifest("V2_READY"))

    inactive = port.dispatch_gui(
        "sync_usage_telemetry",
        [],
        context=context,
        generation=1,
        state="V2_READY",
    )
    assert inactive["ok"] is False
    assert inactive["code"] == "v2_not_active"
    assert calls == []

    port = NativeV2RuntimePort(tmp_path, state_provider=Manifest("V2_ACTIVE"))
    synced = port.dispatch_gui(
        "sync_usage_telemetry",
        [],
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert synced["ok"] is True, synced
    assert calls == [tmp_path.resolve()]
    assert synced["data"] == {
        "status": "error",
        "inserted": 2,
        "rotated": 1,
        "sources": 3,
        "providers": {
            "codex": {"status": "success", "inserted": 2, "rotated": 1, "sources": 1},
            "grok": {"status": "error", "inserted": 0, "rotated": 0, "sources": 2},
        },
    }
    assert "private" not in str(synced)


def test_gui_usage_telemetry_forwards_trusted_scope_and_current_binding_roster(
    tmp_path: Path, monkeypatch
) -> None:
    captured = {}

    class GroupService:
        def list_bindings(self, *, include_inactive=True):
            assert include_inactive is False
            return {
                "bindings": [
                    {
                        "binding_id": "binding-codex",
                        "agent_instance_id": "opaque-codex-id",
                        "share_group_id": "group-a",
                        "provider": "codex",
                        "canonical_program_id": "codex",
                        "display_name": "Codex",
                    },
                    {
                        "binding_id": "binding-other",
                        "agent_instance_id": "opaque-other-id",
                        "share_group_id": "other-group",
                        "provider": "grok",
                        "canonical_program_id": "grok",
                        "display_name": "Grok",
                    },
                ]
            }

    def fake_summary(workspace, **kwargs):
        captured["workspace"] = workspace
        captured.update(kwargs)
        return {"window_days": kwargs["window_days"], "agents": [], "rows": [], "series": []}

    monkeypatch.setattr(usage_telemetry, "get_usage_summary", fake_summary)
    port = NativeV2RuntimePort(tmp_path)
    port._group_control_service = GroupService()
    result = port.dispatch_gui(
        "get_usage_telemetry",
        [30, "codex:codex"],
        context=_context(tmp_path),
        generation=1,
        state="V2_ACTIVE",
    )

    assert result["ok"] is True, result
    assert captured["workspace"] == str(tmp_path.resolve())
    assert captured["window_days"] == 30
    assert captured["share_group_id"] == "group-a"
    assert captured["project_ref"] in ("", None)
    assert captured["agent_key"] == "codex:codex"
    assert captured["agent_roster"] == [
        {
            "provider": "codex",
            "program": "codex",
            "display_name": "Codex",
            "agent_key": "codex:codex",
            "agent_instance_ids": ["opaque-codex-id"],
            "binding_ids": ["binding-codex"],
        }
    ]


def test_gui_usage_telemetry_aggregates_share_group_not_current_project(
    tmp_path: Path, monkeypatch
) -> None:
    _freeze_usage_clock(monkeypatch)
    record_conversion_event(
        tmp_path,
        provider="codex",
        program="codex",
        share_group_id="group-a",
        project_ref="H:/other/project-a",
        observed_at_utc="2026-08-28T01:00:00Z",
        baseline_units=100,
        delivered_units=40,
        event_id="gui-a",
    )
    record_conversion_event(
        tmp_path,
        provider="codex",
        program="codex",
        share_group_id="group-a",
        project_ref="H:/other/project-b",
        observed_at_utc="2026-08-28T01:01:00Z",
        baseline_units=80,
        delivered_units=20,
        event_id="gui-b",
    )
    port = NativeV2RuntimePort(tmp_path)
    result = port.dispatch_gui(
        "get_usage_telemetry",
        [7, ""],
        context=_context(tmp_path),
        generation=1,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    data = result["data"]
    assert data["status"] == "available"
    assert data["share_group_id"] == "group-a"
    assert data["project_ref"] in ("", None)
    assert data["summary"]["estimated_baseline_units"] == 180
    assert data["summary"]["estimated_delivered_units"] == 60
    assert data["summary"]["conversion_count"] == 2
    assert {row["agent_key"] for row in data["rows"]} == {"codex:codex"}
    assert all(row["measurement_state"] == "estimated" for row in data["rows"])


def test_gui_roster_placeholder_does_not_hide_real_codex_row(
    tmp_path: Path, monkeypatch
) -> None:
    _freeze_usage_clock(monkeypatch)
    record_conversion_event(
        tmp_path,
        provider="codex",
        program="codex",
        share_group_id="group-a",
        project_ref="foreign-project",
        observed_at_utc="2026-08-28T01:00:00Z",
        baseline_units=18140,
        delivered_units=14236,
        event_id="gui-real-codex",
    )

    class GroupService:
        def list_bindings(self, *, include_inactive=True):
            return {
                "bindings": [
                    {
                        "binding_id": "binding-codex",
                        "agent_instance_id": "opaque-codex-id",
                        "share_group_id": "group-a",
                        "provider": "codex",
                        "canonical_program_id": "codex",
                        "display_name": "Codex",
                    },
                    {
                        "binding_id": "binding-claude",
                        "agent_instance_id": "opaque-claude-id",
                        "share_group_id": "group-a",
                        "provider": "claude",
                        "canonical_program_id": "claude",
                        "display_name": "Claude",
                    },
                ]
            }

    port = NativeV2RuntimePort(tmp_path)
    port._group_control_service = GroupService()
    result = port.dispatch_gui(
        "get_usage_telemetry",
        [7, ""],
        context=_context(tmp_path),
        generation=1,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    by_key = {agent["agent_key"]: agent for agent in result["data"]["agents"]}
    assert by_key["codex:codex"]["measurement_state"] == "estimated"
    assert by_key["codex:codex"]["estimated_baseline_units"] == 18140
    assert by_key["claude:claude"]["measurement_state"] == "unavailable"
    assert result["data"]["rows"]
    assert all(row["agent_key"] == "codex:codex" for row in result["data"]["rows"])


def test_run_audit_does_not_implicitly_sync_usage_telemetry(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[object] = []

    class FakeAudit:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def audit(self) -> object:
            return SimpleNamespace(
                to_public_dict=lambda: {
                    "status": "PASS",
                    "blocked": False,
                    "blockers": [],
                    "blocker_codes": [],
                    "domains": [],
                    "candidate_count": 0,
                }
            )

    def fake_sync(workspace, **_kwargs):
        calls.append(workspace)
        raise RuntimeError("adapter exploded")

    monkeypatch.setattr(
        "memoryguard.maintenance_v2.reference_audit.ReferenceAudit",
        FakeAudit,
    )
    monkeypatch.setattr(usage_telemetry, "sync_usage_telemetry", fake_sync)
    port = NativeV2RuntimePort(tmp_path)
    context = _context(tmp_path)

    audited = port.dispatch_gui(
        "run_audit",
        [],
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert calls == [], audited
    assert audited["ok"] is True, audited
    assert "usage_sync" not in audited["data"]

    calls.clear()
    queried = port.dispatch_gui(
        "get_audit",
        [],
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert calls == []
    assert queried["ok"] is True, queried
    assert "usage_sync" not in queried.get("data", {})


def test_gui_usage_telemetry_exposes_provider_specific_host_status(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_summary(workspace, **kwargs):
        return {
            "window_days": kwargs["window_days"],
            "status": "available",
            "agents": [
                {
                    "provider": "codex",
                    "program": "codex",
                    "agent_key": "codex:codex",
                    "measurement_state": "estimated",
                    "host_measurement_status": "source_not_found",
                    "host_measurement_reason": "source_not_detected",
                    "estimated_baseline_units": 10,
                    "estimated_delivered_units": 4,
                    "measured_total": None,
                },
                {
                    "provider": "claude",
                    "program": "claude",
                    "agent_key": "claude:claude",
                    "measurement_state": "unavailable",
                    "host_measurement_status": "host_not_supported",
                    "host_measurement_reason": "host_does_not_report_tokens",
                    "measured_total": None,
                },
            ],
            "rows": [
                {
                    "date": "2026-08-28",
                    "provider": "codex",
                    "program": "codex",
                    "agent_key": "codex:codex",
                    "measurement_state": "estimated",
                    "estimated_baseline_units": 10,
                    "estimated_delivered_units": 4,
                    "conversion_count": 1,
                    "measured_total": None,
                }
            ],
            "series": [],
            "summary": {"estimated_baseline_units": 10, "measured_total": None},
        }

    monkeypatch.setattr(usage_telemetry, "get_usage_summary", fake_summary)
    port = NativeV2RuntimePort(tmp_path)
    result = port.dispatch_gui(
        "get_usage_telemetry",
        [7, ""],
        context=_context(tmp_path),
        generation=1,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    by_key = {agent["agent_key"]: agent for agent in result["data"]["agents"]}
    assert by_key["codex:codex"]["host_measurement_status"] == "source_not_found"
    assert by_key["claude:claude"]["host_measurement_reason"] == "host_does_not_report_tokens"
