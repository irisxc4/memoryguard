"""End-to-end Codex + MemoryGuard transport and lifecycle acceptance.

The verifier starts Codex with the user's real MCP configuration, requires live
MemoryGuard status/read tool calls, and concurrently samples the lifecycle
receipt. It never edits MemoryGuard data or lifecycle state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MEMORY_ID = "memory-92583adbc4ddd9a4483020ff14c5eb544ffe89a7"
EXPECTED_SHIM_VERSION = "0.7.1.post6"


def _walk(value: Any):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _find_first(value: Any, key: str) -> Any:
    for item in _walk(value):
        if isinstance(item, dict) and key in item:
            return item[key]
    return None


def _tool_records(events: list[Any], tool_name: str) -> list[dict[str, Any]]:
    keys = ("tool", "tool_name", "name", "method")
    records: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in _walk(events):
        if not isinstance(item, dict):
            continue
        if not any(str(item.get(key) or "") == tool_name for key in keys):
            continue
        marker = id(item)
        if marker in seen:
            continue
        seen.add(marker)
        records.append(item)
    return records


def _nested_payloads(value: Any) -> list[Any]:
    payloads = [value]
    for item in _walk(value):
        if not isinstance(item, str):
            continue
        stripped = item.strip()
        if len(stripped) < 2 or stripped[0] not in "[{":
            continue
        try:
            payloads.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    return payloads


def _record_succeeded(record: dict[str, Any]) -> bool:
    status = str(record.get("status") or "").casefold()
    if status in {"failed", "error", "cancelled", "canceled"}:
        return False
    error = record.get("error")
    return error in (None, "", False, {})


def _runtime_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "MemoryGuard"
    return base / ".memoryguard" / "hook-runtime"


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def _read_lifecycle(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _sample_lifecycle(
    path: Path,
    samples: list[dict[str, Any]],
    seen_updates: set[tuple[int, int]],
) -> None:
    payload = _read_lifecycle(path)
    if payload is None:
        return
    key = (int(payload.get("codex_pid") or 0), int(payload.get("updated_ms") or 0))
    if key in seen_updates:
        return
    seen_updates.add(key)
    samples.append(
        {
            "codex_pid": key[0],
            "updated_ms": key[1],
            "shim_version": str(payload.get("shim_version") or ""),
            "last_event": str(payload.get("last_event") or ""),
            "last_thread_id": str(payload.get("last_thread_id") or ""),
            "last_assigned_cohort": str(payload.get("last_assigned_cohort") or ""),
            "last_assignment_reason": str(payload.get("last_assignment_reason") or ""),
            "last_cohort_count": int(payload.get("last_cohort_count") or 0),
            "last_probe_ms": int(payload.get("last_probe_ms") or 0),
            "last_killed_pids": list(payload.get("last_killed_pids") or []),
            "last_failed_pids": list(payload.get("last_failed_pids") or []),
            "last_snapshot_delta": dict(payload.get("last_snapshot_delta") or {}),
        }
    )


def _lifecycle_acceptance(
    samples: list[dict[str, Any]],
    *,
    baseline_pid: int,
) -> dict[str, Any]:
    candidates = [
        sample
        for sample in samples
        if sample["codex_pid"] > 0
        and sample["shim_version"] == EXPECTED_SHIM_VERSION
    ]
    delta = next(
        (
            sample
            for sample in candidates
            if sample["last_assigned_cohort"]
            and sample["last_assignment_reason"] == "snapshot_delta"
        ),
        None,
    )
    repeated = None
    if delta is not None:
        repeated = next(
            (
                sample
                for sample in candidates
                if sample["codex_pid"] == delta["codex_pid"]
                and sample["updated_ms"] > delta["updated_ms"]
                and sample["last_assigned_cohort"] == delta["last_assigned_cohort"]
                and sample["last_cohort_count"] <= delta["last_cohort_count"]
            ),
            None,
        )
    durable = next(
        (
            sample
            for sample in candidates
            if str((sample.get("last_snapshot_delta") or {}).get("cohort_key") or "")
            and int((sample.get("last_snapshot_delta") or {}).get("assigned_ms") or 0) > 0
            and int((sample.get("last_snapshot_delta") or {}).get("stable_pulse_ms") or 0)
            > int((sample.get("last_snapshot_delta") or {}).get("assigned_ms") or 0)
            and int((sample.get("last_snapshot_delta") or {}).get("stable_cohort_count") or 0)
            <= int((sample.get("last_snapshot_delta") or {}).get("assigned_cohort_count") or 0)
        ),
        None,
    )
    durable_receipt = dict((durable or {}).get("last_snapshot_delta") or {})
    durable_ok = bool(durable_receipt)
    if delta is None and durable is not None:
        delta = durable
    if repeated is None and durable is not None:
        repeated = durable
    return {
        "ok": (delta is not None and repeated is not None) or durable_ok,
        "baseline_codex_pid": baseline_pid,
        "accepted_codex_pid": int((delta or durable or {}).get("codex_pid") or 0),
        "accepted_baseline_generation": bool(
            (delta or durable) and int((delta or durable or {}).get("codex_pid") or 0) == baseline_pid
        ),
        "sample_count": len(samples),
        "candidate_sample_count": len(candidates),
        "delta_sample": delta or {},
        "repeated_pulse_sample": repeated or {},
        "cohort_nonempty": bool(delta and delta["last_assigned_cohort"]),
        "assignment_reason_snapshot_delta": bool(
            delta and delta["last_assignment_reason"] == "snapshot_delta"
        ),
        "repeated_pulse_no_growth": bool(repeated),
        "durable_snapshot_delta_receipt": durable_receipt,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--memory-id", default=DEFAULT_MEMORY_ID)
    parser.add_argument("--cwd", default=str(Path.cwd()))
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args(argv)

    runtime_root = _runtime_root()
    lifecycle_path = runtime_root / "codex-mcp-lifecycle.json"
    baseline = _read_lifecycle(lifecycle_path) or {}
    baseline_pid = int(baseline.get("codex_pid") or 0)

    prompt = (
        "只执行 MemoryGuard MCP 实机验收，不读取项目文件，不修改任何内容。"
        "必须先调用 memoryguard_memory_status，再调用 memoryguard_memory_read，"
        f"memory_id={args.memory_id}。两个工具调用之间至少等待六秒，确保产生两个非节流"
        "生命周期 pulse。最后输出一行 JSON，字段为 status_ok、memory_id、"
        "injection_policy、body_length。不得省略工具调用。"
    )
    command = [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-C",
        str(Path(args.cwd).expanduser().resolve(strict=False)),
        prompt,
    ]

    stdout_file = tempfile.NamedTemporaryFile(prefix="mg-codex-live-", suffix=".stdout", delete=False)
    stderr_file = tempfile.NamedTemporaryFile(prefix="mg-codex-live-", suffix=".stderr", delete=False)
    stdout_path = Path(stdout_file.name)
    stderr_path = Path(stderr_file.name)
    stdout_file.close()
    stderr_file.close()

    samples: list[dict[str, Any]] = []
    seen_updates: set[tuple[int, int]] = set()
    timed_out = False
    process: subprocess.Popen[bytes] | None = None
    started = time.monotonic()
    try:
        with stdout_path.open("wb") as stdout_stream, stderr_path.open("wb") as stderr_stream:
            process = subprocess.Popen(
                command,
                stdout=stdout_stream,
                stderr=stderr_stream,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            while process.poll() is None:
                _sample_lifecycle(lifecycle_path, samples, seen_updates)
                if time.monotonic() - started > max(30.0, args.timeout):
                    timed_out = True
                    process.terminate()
                    break
                time.sleep(0.1)
            try:
                process.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
            # Hook state writes can land just after the CLI process exits.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                _sample_lifecycle(lifecycle_path, samples, seen_updates)
                time.sleep(0.1)
        return_code = int(process.returncode or 0)
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    finally:
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)

    events: list[Any] = []
    invalid_lines = 0
    for line in stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            invalid_lines += 1
    combined = stdout + "\n" + stderr
    folded = combined.casefold()
    transport_closed = "transport closed" in folded or "transport_closed" in folded
    status_records = _tool_records(events, "memoryguard_memory_status")
    read_records = _tool_records(events, "memoryguard_memory_read")
    status_called = any(_record_succeeded(record) for record in status_records)
    read_called = any(_record_succeeded(record) for record in read_records)

    status_ok = False
    for record in status_records:
        for payload in _nested_payloads(record):
            if bool(_find_first(payload, "ok")) and str(_find_first(payload, "state") or "") == "V2_ACTIVE":
                status_ok = True
                break
        if status_ok:
            break

    memory_present = False
    policy = ""
    body = ""
    for record in read_records:
        for payload in _nested_payloads(record):
            returned_id = str(_find_first(payload, "memory_id") or "")
            candidate_policy = str(_find_first(payload, "injection_policy") or "")
            candidate_body = str(_find_first(payload, "body") or "")
            if returned_id == args.memory_id:
                memory_present = True
                policy = candidate_policy or policy
                body = candidate_body or body
        if memory_present and policy and body:
            break

    lifecycle = _lifecycle_acceptance(samples, baseline_pid=baseline_pid)
    transport_ok = (
        not timed_out
        and return_code == 0
        and not transport_closed
        and status_called
        and status_ok
        and read_called
        and memory_present
        and policy == "relevant"
        and bool(body)
    )
    ok = transport_ok and bool(lifecycle["ok"])
    receipt = {
        "version": 2,
        "at": datetime.now(timezone.utc).isoformat(),
        "ok": ok,
        "transport_ok": transport_ok,
        "return_code": return_code,
        "timed_out": timed_out,
        "transport_closed": transport_closed,
        "status_called": status_called,
        "status_ok": status_ok,
        "read_called": read_called,
        "memory_id": args.memory_id,
        "memory_present": memory_present,
        "injection_policy": policy,
        "body_length": len(body),
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest() if body else "",
        "event_count": len(events),
        "invalid_json_line_count": invalid_lines,
        "lifecycle": lifecycle,
        "stderr_tail": stderr.splitlines()[-20:],
    }
    path = runtime_root / "codex-memoryguard-live-acceptance.json"
    _write_receipt(path, receipt)
    print(json.dumps({**receipt, "receipt_path": str(path)}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
