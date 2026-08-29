from __future__ import annotations

from pathlib import Path

import memoryguard.host_hooks as host_hooks
import memoryguard.usage_telemetry as usage_telemetry
from memoryguard.runtime_v2.context_engine import ContextEngine


def test_context_engine_exposes_body_units_and_unknown_wrapper_as_null() -> None:
    packet = ContextEngine(state="V2_ACTIVE", ready=True).bootstrap(
        {"agent": "agent-a", "max_items": 1},
        {
            "relevant": [
                {"id": "first", "body": "alpha"},
                {"id": "second", "body": "bravo"},
            ]
        },
    )

    usage = packet.usage
    assert usage["measurement_basis"] == "mg_deterministic_unit"
    assert usage["candidate_body_units"] == 10
    assert usage["delivered_body_units"] == 5
    assert usage["saved_body_units"] == 5
    assert usage["baseline_total_units"] is None
    assert usage["delivered_total_units"] is None
    assert usage["wrapper_overhead_units"] is None
    assert usage["baseline_units"] is None
    assert usage["delivered_units"] is None


def test_hook_records_final_wrapper_with_same_deterministic_basis(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict] = []

    def record(*args, **kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(usage_telemetry, "record_conversion_event", record)
    body = "正文"
    final_text = "[MemoryGuard]\n" + body
    host_hooks._record_conversion_usage(
        workspace=tmp_path,
        provider="grok",
        event="session_start",
        text=final_text,
        packet={
            "usage": {
                "measurement_basis": "mg_deterministic_unit",
                "candidate_body_units": 20,
                "delivered_body_units": len(body),
            }
        },
        context_identity={
            "provider": "grok",
            "share_group_id": "shared-group",
            "project_ref": "project-ref",
            "runtime_role": "root",
            "context_hash": "ctx-hash",
        },
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["provider"] == "grok"
    assert call["program"] == "grok"
    assert call["share_group_id"] == "shared-group"
    assert call["project_ref"] == "project-ref"
    assert call["measurement_basis"] == "mg_deterministic_unit"
    assert call["baseline_units"] == len(final_text) + (20 - len(body))
    assert call["delivered_units"] == len(final_text)
    assert "body" not in call


def test_hook_clamps_negative_body_savings_without_guessing_wrapper(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        usage_telemetry,
        "record_conversion_event",
        lambda *args, **kwargs: calls.append(kwargs),
    )

    host_hooks._record_conversion_usage(
        workspace=tmp_path,
        provider="codex",
        event="user_prompt",
        text="abcd",
        packet={
            "usage": {
                "candidate_body_units": 3,
                "delivered_body_units": 6,
            }
        },
        context_identity={
            "provider": "codex",
            "share_group_id": "group-a",
            "project_ref": "project-a",
        },
    )

    assert len(calls) == 1
    assert calls[0]["baseline_units"] == 4
    assert calls[0]["delivered_units"] == 4


def test_hook_does_not_record_when_body_measurements_are_unknown(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        usage_telemetry,
        "record_conversion_event",
        lambda *args, **kwargs: calls.append(kwargs),
    )

    host_hooks._record_conversion_usage(
        workspace=tmp_path,
        provider="codex",
        event="session_start",
        text="wrapper only",
        packet={"usage": {"baseline_units": 100}},
        context_identity={"provider": "codex"},
    )

    assert calls == []


def test_hook_does_not_mix_measurement_bases(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        usage_telemetry,
        "record_conversion_event",
        lambda *args, **kwargs: calls.append(kwargs),
    )

    host_hooks._record_conversion_usage(
        workspace=tmp_path,
        provider="codex",
        event="session_start",
        text="wrapper",
        packet={
            "usage": {
                "measurement_basis": "provider_reported_token",
                "candidate_body_units": 10,
                "delivered_body_units": 5,
            }
        },
        context_identity={"provider": "codex"},
    )

    assert calls == []
