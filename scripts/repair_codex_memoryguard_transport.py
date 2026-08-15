"""Repair and verify the Codex -> MemoryGuard stdio MCP transport.

The script is intentionally independent from the MemoryGuard server process. It:

* removes duplicate/stale ``mcp_servers.memoryguard`` TOML sections;
* preserves the trusted MemoryGuard environment binding;
* pins UTF-8 stdio and the current Python interpreter;
* atomically rewrites ``~/.codex/config.toml`` with a timestamped backup;
* launches the configured server and performs JSON-RPC initialize/list/status/read;
* writes a non-sensitive verification receipt under the MemoryGuard runtime dir.

It never mutates MemoryGuard data or Codex conversation state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from memoryguard import toml_compat as tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPAIR_VERSION = "1"
DEFAULT_MEMORY_ID = "memory-92583adbc4ddd9a4483020ff14c5eb544ffe89a7"
_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")


@dataclass(frozen=True, slots=True)
class ProbeResult:
    ok: bool
    initialized: bool
    tools_listed: bool
    status_ok: bool
    read_ok: bool
    memory_id: str
    injection_policy: str
    body_length: int
    body_sha256: str
    body_matches_store: bool | None
    policy_matches_store: bool | None
    stderr_tail: tuple[str, ...]
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "initialized": self.initialized,
            "tools_listed": self.tools_listed,
            "status_ok": self.status_ok,
            "read_ok": self.read_ok,
            "memory_id": self.memory_id,
            "injection_policy": self.injection_policy,
            "body_length": self.body_length,
            "body_sha256": self.body_sha256,
            "body_matches_store": self.body_matches_store,
            "policy_matches_store": self.policy_matches_store,
            "stderr_tail": list(self.stderr_tail),
            "error": self.error,
        }


def _codex_config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve(strict=False)
    home = Path(os.environ.get("CODEX_HOME", "") or (Path.home() / ".codex"))
    return home.expanduser().resolve(strict=False) / "config.toml"


def _normalize_section(raw: str) -> str:
    parts = [part.strip().strip('"').strip("'") for part in raw.split(".")]
    return ".".join(parts).casefold()


def _is_memoryguard_section(raw: str) -> bool:
    normalized = _normalize_section(raw)
    return normalized == "mcp_servers.memoryguard" or normalized.startswith(
        "mcp_servers.memoryguard."
    )


def _strip_memoryguard_sections(text: str) -> str:
    kept: list[str] = []
    skip = False
    for line in text.splitlines(keepends=True):
        match = _SECTION_RE.match(line)
        if match:
            skip = _is_memoryguard_section(match.group(1))
        if not skip:
            kept.append(line)
    return "".join(kept).rstrip() + "\n"


def _toml_string(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return _toml_string(value)


def _parse_toml_assignment(line: str) -> tuple[str, Any] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, raw = stripped.split("=", 1)
    key = key.strip().strip('"').strip("'")
    if not key:
        return None
    try:
        value = tomllib.loads("value = " + raw)["value"]
    except (tomllib.TOMLDecodeError, KeyError):
        return None
    return key, value


def _recover_memoryguard_server(text: str) -> dict[str, Any]:
    """Recover simple MemoryGuard sections even when duplicate TOML is invalid."""
    server: dict[str, Any] = {}
    environment: dict[str, Any] = {}
    active_section = ""
    for line in text.splitlines():
        match = _SECTION_RE.match(line)
        if match:
            normalized = _normalize_section(match.group(1))
            if normalized == "mcp_servers.memoryguard":
                active_section = "server"
            elif normalized == "mcp_servers.memoryguard.env":
                active_section = "env"
            else:
                active_section = ""
            continue
        if not active_section:
            continue
        assignment = _parse_toml_assignment(line)
        if assignment is None:
            continue
        key, value = assignment
        if active_section == "env":
            environment[key] = value
        else:
            server[key] = value
    if environment:
        server["env"] = environment
    if not server:
        raise RuntimeError("codex_memoryguard_server_missing")
    return server


def _load_server_config(text: str) -> dict[str, Any]:
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return _recover_memoryguard_server(text)
    servers = parsed.get("mcp_servers")
    if not isinstance(servers, dict):
        raise RuntimeError("codex_mcp_servers_missing")
    server = servers.get("memoryguard")
    if not isinstance(server, dict):
        raise RuntimeError("codex_memoryguard_server_missing")
    return dict(server)


def _canonical_block(server: dict[str, Any]) -> str:
    environment = server.get("env")
    env = dict(environment) if isinstance(environment, dict) else {}
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    lines = [
        "[mcp_servers.memoryguard]",
        f"command = {_toml_string(sys.executable)}",
        'args = ["-X", "utf8", "-m", "memoryguard.mcp_server"]',
        "enabled = true",
        f"startup_timeout_sec = {_toml_value(int(server.get('startup_timeout_sec', 30) or 30))}",
        f"tool_timeout_sec = {_toml_value(int(server.get('tool_timeout_sec', 120) or 120))}",
    ]
    cwd = server.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        lines.append(f"cwd = {_toml_string(cwd)}")
    lines.extend(["", "[mcp_servers.memoryguard.env]"])
    for key in sorted(env):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)):
            continue
        lines.append(f"{key} = {_toml_string(env[key])}")
    return "\n".join(lines) + "\n"


def _prune_backups(path: Path, *, keep: int = 3) -> None:
    backups = sorted(
        path.parent.glob(path.name + ".memoryguard-transport-*.bak"),
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    )
    for stale in backups[max(0, keep):]:
        try:
            stale.unlink()
        except OSError:
            pass


def repair_config(path: Path, *, touch: bool = False) -> tuple[dict[str, Any], bool, Path | None]:
    original = path.read_text(encoding="utf-8-sig")
    server = _load_server_config(original)
    repaired = _strip_memoryguard_sections(original) + "\n" + _canonical_block(server)
    parsed = tomllib.loads(repaired)
    canonical = parsed["mcp_servers"]["memoryguard"]
    if canonical.get("args") != ["-X", "utf8", "-m", "memoryguard.mcp_server"]:
        raise RuntimeError("canonical_args_validation_failed")
    env = canonical.get("env") or {}
    if env.get("PYTHONUTF8") != "1" or env.get("PYTHONIOENCODING") != "utf-8":
        raise RuntimeError("canonical_utf8_validation_failed")

    changed = repaired != original
    backup: Path | None = None
    if changed or touch:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup = path.with_name(path.name + f".memoryguard-transport-{stamp}.bak")
        shutil.copy2(path, backup)
        fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(repaired)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
            _prune_backups(path)
        finally:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
    return dict(canonical), changed, backup


def _reader(stream: Any, output: queue.Queue[str]) -> None:
    try:
        for line in iter(stream.readline, ""):
            output.put(line.rstrip("\r\n"))
    finally:
        output.put("__MEMORYGUARD_STREAM_EOF__")


def _wait_response(
    output: queue.Queue[str],
    request_id: int,
    *,
    timeout: float,
    contamination: list[str],
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = output.get(timeout=min(0.25, max(0.01, deadline - time.monotonic())))
        except queue.Empty:
            continue
        if line == "__MEMORYGUARD_STREAM_EOF__":
            raise RuntimeError("transport_closed_before_response")
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            contamination.append(line[:500])
            continue
        if payload.get("id") == request_id:
            return payload
    raise TimeoutError(f"mcp_response_timeout:{request_id}")


def _tool_payload(response: dict[str, Any]) -> dict[str, Any]:
    if response.get("error"):
        raise RuntimeError("mcp_tool_error:" + json.dumps(response["error"], ensure_ascii=False))
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("mcp_result_missing")
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                try:
                    decoded = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(decoded, dict):
                    return decoded
    return result


def _find_first(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = _find_first(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_first(child, key)
            if found is not None:
                return found
    return None


def _stored_memory(environment: dict[str, str], memory_id: str) -> tuple[str, str] | None:
    workspace_text = str(
        environment.get("MEMORYGUARD_WORKSPACE")
        or environment.get("MEMORYGUARD_CONTROL_WORKSPACE")
        or ""
    ).strip()
    if not workspace_text:
        return None
    database = Path(workspace_text).expanduser() / ".memoryguard" / "memory" / "memory.db"
    if not database.is_file():
        return None
    try:
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.5)
        row = connection.execute(
            "SELECT body, injection_policy FROM atoms WHERE memory_id=? "
            "ORDER BY revision DESC LIMIT 1",
            (memory_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        try:
            connection.close()
        except (NameError, sqlite3.Error):
            pass
    if row is None:
        return None
    return str(row[0] or ""), str(row[1] or "")


def probe_transport(
    server: dict[str, Any],
    *,
    memory_id: str,
    timeout: float = 15.0,
) -> ProbeResult:
    command = str(server.get("command") or "").strip()
    args = [str(item) for item in server.get("args") or []]
    if not command:
        raise RuntimeError("memoryguard_command_missing")
    environment = os.environ.copy()
    configured_env = server.get("env")
    if isinstance(configured_env, dict):
        environment.update({str(key): str(value) for key, value in configured_env.items()})
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"

    stdout_lines: queue.Queue[str] = queue.Queue()
    stderr_lines: queue.Queue[str] = queue.Queue()
    contamination: list[str] = []
    stderr_tail: list[str] = []
    process = subprocess.Popen(
        [command, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        cwd=str(server.get("cwd") or Path.cwd()),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    threading.Thread(target=_reader, args=(process.stdout, stdout_lines), daemon=True).start()
    threading.Thread(target=_reader, args=(process.stderr, stderr_lines), daemon=True).start()

    def send(payload: dict[str, Any]) -> None:
        process.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        process.stdin.flush()

    initialized = tools_listed = status_ok = read_ok = False
    policy = ""
    body = ""
    error = ""
    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "memoryguard-transport-repair", "version": REPAIR_VERSION},
                },
            }
        )
        initialize = _wait_response(stdout_lines, 1, timeout=timeout, contamination=contamination)
        initialized = isinstance(initialize.get("result"), dict) and not initialize.get("error")
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = _wait_response(stdout_lines, 2, timeout=timeout, contamination=contamination)
        names = {
            str(item.get("name") or "")
            for item in ((tools.get("result") or {}).get("tools") or [])
            if isinstance(item, dict)
        }
        tools_listed = {
            "memoryguard_memory_status",
            "memoryguard_memory_read",
        }.issubset(names)

        send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "memoryguard_memory_status", "arguments": {}},
            }
        )
        status_payload = _tool_payload(
            _wait_response(stdout_lines, 3, timeout=timeout, contamination=contamination)
        )
        status_ok = bool(status_payload.get("ok")) and str(status_payload.get("state") or "") == "V2_ACTIVE"

        send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "memoryguard_memory_read",
                    "arguments": {"memory_id": memory_id},
                },
            }
        )
        read_payload = _tool_payload(
            _wait_response(stdout_lines, 4, timeout=timeout, contamination=contamination)
        )
        returned_id = str(_find_first(read_payload, "memory_id") or "")
        body = str(_find_first(read_payload, "body") or "")
        policy = str(_find_first(read_payload, "injection_policy") or "")
        read_ok = bool(read_payload.get("ok")) and returned_id == memory_id and bool(body)
    except Exception as exc:
        error = f"{type(exc).__name__}:{exc}"
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
        while True:
            try:
                line = stderr_lines.get_nowait()
            except queue.Empty:
                break
            if line != "__MEMORYGUARD_STREAM_EOF__":
                stderr_tail.append(line[:1000])
        stderr_tail.extend(contamination)
        stderr_tail = stderr_tail[-20:]

    stored = _stored_memory(environment, memory_id)
    body_matches: bool | None = None
    policy_matches: bool | None = None
    if stored is not None and body:
        body_matches = stored[0] == body
        policy_matches = stored[1] == policy
    ok = (
        initialized
        and tools_listed
        and status_ok
        and read_ok
        and policy == "relevant"
        and body_matches is not False
        and policy_matches is not False
        and not error
    )
    return ProbeResult(
        ok=ok,
        initialized=initialized,
        tools_listed=tools_listed,
        status_ok=status_ok,
        read_ok=read_ok,
        memory_id=memory_id,
        injection_policy=policy,
        body_length=len(body),
        body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest() if body else "",
        body_matches_store=body_matches,
        policy_matches_store=policy_matches,
        stderr_tail=tuple(stderr_tail),
        error=error,
    )


def _receipt_path(server: dict[str, Any]) -> Path:
    environment = server.get("env") if isinstance(server.get("env"), dict) else {}
    workspace = str(
        environment.get("MEMORYGUARD_WORKSPACE")
        or environment.get("MEMORYGUARD_CONTROL_WORKSPACE")
        or (Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "MemoryGuard")
    )
    return Path(workspace).expanduser() / ".memoryguard" / "hook-runtime" / "codex-mcp-transport-repair.json"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--memory-id", default=DEFAULT_MEMORY_ID)
    parser.add_argument("--touch", action="store_true")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)

    config_path = _codex_config_path(args.config)
    server, changed, backup = repair_config(config_path, touch=args.touch)
    result = probe_transport(server, memory_id=args.memory_id, timeout=max(1.0, args.timeout))
    receipt = {
        "version": REPAIR_VERSION,
        "at": datetime.now(timezone.utc).isoformat(),
        "config_path": str(config_path),
        "config_changed": changed,
        "backup_path": str(backup) if backup else "",
        "command": str(server.get("command") or ""),
        "args": list(server.get("args") or []),
        "probe": result.to_dict(),
    }
    receipt_path = _receipt_path(server)
    _write_receipt(receipt_path, receipt)
    print(json.dumps({**receipt, "receipt_path": str(receipt_path)}, ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
