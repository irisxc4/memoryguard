from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_codex_memoryguard_live.py"
SPEC = importlib.util.spec_from_file_location("verify_codex_memoryguard_live", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
verify = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verify
SPEC.loader.exec_module(verify)


def test_prompt_text_does_not_count_as_tool_call():
    events = [
        {
            "type": "user_message",
            "text": (
                "call memoryguard_memory_status and memoryguard_memory_read "
                "memory-92583adbc4ddd9a4483020ff14c5eb544ffe89a7"
            ),
        }
    ]
    assert verify._tool_records(events, "memoryguard_memory_status") == []
    assert verify._tool_records(events, "memoryguard_memory_read") == []


def test_structured_mcp_records_and_nested_results_are_detected():
    events = [
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "tool": "memoryguard_memory_status",
                "status": "completed",
                "result": '{"ok":true,"state":"V2_ACTIVE"}',
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "tool": "memoryguard_memory_read",
                "status": "completed",
                "result": (
                    '{"ok":true,"data":{"memory_id":'
                    '"memory-92583adbc4ddd9a4483020ff14c5eb544ffe89a7",'
                    '"body":"stored body","injection_policy":"relevant"}}'
                ),
            },
        },
    ]
    status = verify._tool_records(events, "memoryguard_memory_status")
    read = verify._tool_records(events, "memoryguard_memory_read")
    assert len(status) == 1 and verify._record_succeeded(status[0])
    assert len(read) == 1 and verify._record_succeeded(read[0])
    nested = verify._nested_payloads(read[0])
    assert verify._find_first(nested, "injection_policy") == "relevant"
    assert verify._find_first(nested, "body") == "stored body"


def test_current_baseline_generation_can_supply_durable_lifecycle_receipt():
    samples = [
        {
            "codex_pid": 100,
            "updated_ms": 20,
            "shim_version": verify.EXPECTED_SHIM_VERSION,
            "last_event": "post_tool",
            "last_thread_id": "thread-a",
            "last_assigned_cohort": "300:1000",
            "last_assignment_reason": "snapshot_delta",
            "last_cohort_count": 2,
            "last_probe_ms": 20,
            "last_killed_pids": [],
            "last_failed_pids": [],
            "last_snapshot_delta": {
                "thread_id": "thread-a",
                "cohort_key": "300:1000",
                "assigned_ms": 10,
                "assigned_cohort_count": 2,
                "stable_pulse_ms": 20,
                "stable_cohort_count": 2,
            },
        }
    ]
    result = verify._lifecycle_acceptance(samples, baseline_pid=100)
    assert result["ok"] is True
    assert result["accepted_codex_pid"] == 100
    assert result["accepted_baseline_generation"] is True


def test_durable_snapshot_delta_receipt_satisfies_lifecycle_acceptance():
    samples = [
        {
            "codex_pid": 200,
            "updated_ms": 20,
            "shim_version": verify.EXPECTED_SHIM_VERSION,
            "last_event": "post_tool",
            "last_thread_id": "thread-a",
            "last_assigned_cohort": "300:1000",
            "last_assignment_reason": "snapshot_delta",
            "last_cohort_count": 3,
            "last_probe_ms": 20,
            "last_killed_pids": [],
            "last_failed_pids": [],
            "last_snapshot_delta": {
                "thread_id": "thread-a",
                "cohort_key": "300:1000",
                "assigned_ms": 10,
                "assigned_cohort_count": 3,
                "stable_pulse_ms": 20,
                "stable_cohort_count": 3,
            },
        }
    ]
    result = verify._lifecycle_acceptance(samples, baseline_pid=100)
    assert result["ok"] is True
    assert result["cohort_nonempty"] is True
    assert result["assignment_reason_snapshot_delta"] is True
    assert result["repeated_pulse_no_growth"] is True
