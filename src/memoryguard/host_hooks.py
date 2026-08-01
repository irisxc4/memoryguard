"""User-level host hooks for deterministic MemoryGuard takeover.

The Skill is the installer/trigger.  Enforcement lives in host lifecycle hooks
because a Skill only runs while it is active.  This module keeps host-specific
configuration behind one small interface and keeps runtime receipts free of
prompt or memory bodies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HOOK_VERSION = "1"
HOOK_MARKER = "memoryguard.host_hooks"
HOOK_MODES = {"enforce", "observe", "paused"}
_RUNTIME_DIR = "hook-runtime"
_MAX_RECEIPT_AGE_DAYS = 30
_COMPACT_REMINDER = (
    "压缩前发现尚未沉淀的长期记忆候选；继续工作前用 "
    "memoryguard_memory_write 萃取保存，不得保存整段对话。"
)

_EVENT_NAMES = {
    "session_start": "SessionStart",
    "subagent_start": "SubagentStart",
    "user_prompt": "UserPromptSubmit",
    "pre_tool": "PreToolUse",
    "post_tool": "PostToolUse",
    "pre_compact": "PreCompact",
    "stop": "Stop",
}

_CURSOR_EVENT_NAMES = {
    "session_start": "sessionStart",
    "user_prompt": "beforeSubmitPrompt",
    "pre_tool": "preToolUse",
    "post_tool": "postToolUse",
    "pre_compact": "preCompact",
    "stop": "stop",
}

_DURABLE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bremember\b",
        r"\bfrom now on\b",
        r"\bmy preference\b",
        r"\bdefault to\b",
        r"\bnever again\b",
        r"记住",
        r"以后(?:都|默认|不要|必须)",
        r"从今以后",
        r"默认(?:使用|走|采用)",
        r"我(?:喜欢|偏好|习惯)",
        r"不要再",
        r"长期规则",
        r"更正[：:]",
    )
)

_NATIVE_PATH_PATTERNS = (
    re.compile(r"/\.codex/memories(?:/|$)", re.IGNORECASE),
    re.compile(r"/\.claude/projects/.+/memory(?:/|$)", re.IGNORECASE),
    re.compile(r"/\.cursor/memories(?:/|$)", re.IGNORECASE),
    re.compile(r"/\.trae(?:-cn)?/.*/memory(?:/|$)", re.IGNORECASE),
)

_SHELL_WRITE_PATTERN = re.compile(
    r"(?:^|[\s;&|])(?:rm|mv|cp|mkdir|del|move|copy|"
    r"remove-item|move-item|copy-item|new-item|set-content|add-content|out-file)"
    r"(?:[\s;&|]|$)|(?:>>?|2>)",
    re.IGNORECASE,
)



def read_hook_stdin_json(stdin_buffer=None):
    """Decode hook stdin JSON. utf-8-sig strips BOM (Cursor) and accepts plain UTF-8 (Codex)."""
    import json
    import sys
    buf = sys.stdin.buffer if stdin_buffer is None else stdin_buffer
    raw = buf.read()
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8-sig"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _effective_agent_context(
    provider: str,
    agent_instance_id: str,
    share_group_id: str,
    payload: dict[str, Any],
    *,
    event: str,
):
    """Build scope from host event identity, never a prompt-supplied role."""
    from .schema_v3 import EffectiveAgentContext
    from .rule_scope import canonical_project_ref
    return EffectiveAgentContext(
        agent_instance_id=agent_instance_id,
        share_group_id=share_group_id,
        provider=provider,
        project_ref=canonical_project_ref(
            payload.get("project_ref") or payload.get("cwd")
        ),
        runtime_role="subagent" if event == "subagent_start" else "root",
        runtime_agent_id=(
            str(payload.get("subagent_id") or "")
            if event == "subagent_start" else ""
        ),
        parent_agent_id=(
            agent_instance_id if event == "subagent_start" else ""
        ),
    )


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:24]


@contextmanager
def _cross_process_path_lock(path: Path, timeout_seconds: float = 3.0):
    """Serialize atomic replacements across Hook processes.

    Windows denies replacing a destination while another process still has a
    transient handle.  A sidecar byte lock prevents our own writers racing;
    bounded replace retries cover short-lived antivirus/indexer handles.
    """
    lock_path = path.with_name(f".{path.name}.memoryguard.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd: int | None = None
    deadline = time.monotonic() + timeout_seconds
    try:
        while lock_fd is None:
            try:
                lock_fd = os.open(
                    lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                os.write(lock_fd, f"{os.getpid()} {_now_iso()}".encode("ascii"))
            except (FileExistsError, PermissionError):
                # Recover only genuinely stale crash remnants.  Normal
                # writers hold the lock for milliseconds and are never
                # unlinked by a waiter.
                try:
                    stale = time.time() - lock_path.stat().st_mtime > 30.0
                    if stale:
                        lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out acquiring hook runtime lock: {path}")
                time.sleep(0.01)
        yield
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            for attempt in range(5):
                try:
                    lock_path.unlink(missing_ok=True)
                    break
                except PermissionError:
                    if attempt == 4:
                        break
                    time.sleep(0.01 * (attempt + 1))


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _cross_process_path_lock(path):
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.memoryguard-",
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            for attempt in range(7):
                try:
                    os.replace(tmp, path)
                    break
                except OSError as exc:
                    retryable = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32}
                    if not retryable or attempt == 6:
                        raise
                    time.sleep(0.01 * (2 ** attempt))
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def _load_json_config(path: Path, *, strict: bool) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        if strict:
            raise ValueError(f"invalid JSON hook config: {path}: {exc}") from exc
        return {}
    if not isinstance(data, dict):
        if strict:
            raise ValueError(f"invalid JSON hook config root: {path}")
        return {}
    return data


def _coerce_tool_result_status(payload: Any) -> tuple[bool | None, str | None]:
    """Return ``(ok, reason)`` for tool execution feedback.

    ``ok`` is ``True``/``False`` for explicit success/failure; ``None`` means
    cannot determine from current payload.
    """
    if payload is None:
        return None, None

    if isinstance(payload, dict):
        if "isError" in payload:
            is_error = payload.get("isError")
            if isinstance(is_error, bool):
                return (False, "isError=true") if is_error else (True, None)
        for key in ("error", "error_code", "message"):
            if payload.get(key):
                if key == "error":
                    if isinstance(payload[key], (dict, list, str)):
                        return False, "error present"
                else:
                    return False, f"{key}={payload[key]!r}"
        ok_value = payload.get("ok")
        if isinstance(ok_value, bool):
            return (True, None) if ok_value else (False, "ok=false")
        if payload.get("status") in {"error", "failed", "failure"}:
            return False, f"status={payload.get('status')}"
        positive_signals = ("memory_id", "decision_id", "version_id", "record")
        if any(sig in payload for sig in positive_signals):
            return True, None
        content = payload.get("content")
        if isinstance(content, list) and content:
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if not isinstance(text, str):
                    continue
                try:
                    parsed = json.loads(text)
                except Exception:
                    continue
                status, reason = _coerce_tool_result_status(parsed)
                if status is not None:
                    return status, reason
        return None, None

    if isinstance(payload, list):
        unknown_count = 0
        for item in payload:
            status, reason = _coerce_tool_result_status(item)
            if status is not None:
                return status, reason
            unknown_count += 1
        if unknown_count:
            return None, None

    return None, None


def _write_json_config(path: Path, data: dict[str, Any]) -> None:
    if not data:
        path.unlink(missing_ok=True)
        return
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _command(
    provider: str,
    event: str,
    workspace: Path,
    agent_instance_id: str,
    share_group_id: str,
    *,
    windows: bool | None = None,
) -> str:
    argv = [
        "python",
        "-X",
        "utf8",
        "-m",
        HOOK_MARKER,
        "run",
        "--provider",
        provider,
        "--event",
        event,
        "--workspace",
        str(workspace),
        "--agent-id",
        agent_instance_id,
        "--share-group-id",
        share_group_id,
        "--managed-by",
        "memoryguard",
    ]
    if windows is None:
        windows = os.name == "nt"
    if windows:
        return subprocess.list2cmdline(argv)
    import shlex

    return shlex.join(argv)


def _is_our_handler(handler: Any) -> bool:
    if not isinstance(handler, dict):
        return False
    command = str(handler.get("command", "") or "")
    return HOOK_MARKER in command and "--managed-by" in command


def _owned_hook_hash(data: dict[str, Any]) -> str:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return ""
    owned: list[dict[str, Any]] = []
    for event_name in sorted(hooks):
        entries = hooks.get(event_name)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if _is_our_handler(entry):
                owned.append({"event": event_name, "handler": entry})
                continue
            if not isinstance(entry, dict):
                continue
            handlers = entry.get("hooks")
            if not isinstance(handlers, list):
                continue
            owned_handlers = [
                handler for handler in handlers if _is_our_handler(handler)
            ]
            if owned_handlers:
                owned.append({
                    "event": event_name,
                    "group": {
                        key: value
                        for key, value in entry.items()
                        if key != "hooks"
                    },
                    "handlers": owned_handlers,
                })
    if not owned:
        return ""
    serialized = json.dumps(
        owned,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _owned_agent_ids(data: dict[str, Any]) -> set[str]:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return set()
    handlers: list[dict[str, Any]] = []
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if _is_our_handler(entry):
                handlers.append(entry)
                continue
            if not isinstance(entry, dict):
                continue
            nested = entry.get("hooks")
            if isinstance(nested, list):
                handlers.extend(
                    handler for handler in nested if _is_our_handler(handler)
                )
    result: set[str] = set()
    pattern = re.compile(
        r"""--agent-id(?:=|\s+)(?:"([^"]+)"|'([^']+)'|([^\s]+))"""
    )
    for handler in handlers:
        command = str(handler.get("commandWindows", "") or "")
        if not command:
            command = str(handler.get("command", "") or "")
        match = pattern.search(command)
        if match:
            result.add(next(value for value in match.groups() if value))
    return result


def _validate_binding(
    workspace: Path,
    agent_instance_id: str,
    share_group_id: str,
) -> None:
    if not agent_instance_id:
        raise ValueError("agent_instance_id is required for hook installation")
    if not share_group_id:
        raise ValueError("share_group_id is required for hook installation")
    from .agent_binding import AgentBindingStore

    bindings = AgentBindingStore(workspace).find_by_agent(
        agent_instance_id, include_inactive=False,
    )
    if not any(binding.share_group_id == share_group_id for binding in bindings):
        raise ValueError(
            "active binding not found for "
            f"agent_instance_id={agent_instance_id!r}, "
            f"share_group_id={share_group_id!r}"
        )


@dataclass(frozen=True)
class HookCapability:
    provider: str
    supported: bool
    config_file: str
    events: tuple[str, ...]
    context_mode: str
    native_write_guard: str
    stop_guard: str
    requires_restart: bool
    requires_trust: bool
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["events"] = list(self.events)
        data["notes"] = list(self.notes)
        return data


class HostHookAdapter:
    """Small interface; host JSON shape and runtime semantics stay inside."""

    provider = "base"

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).expanduser().resolve()

    def config_path(self) -> Path:
        raise NotImplementedError

    def capability(self) -> HookCapability:
        raise NotImplementedError

    def _remove_owned(self, data: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _add_owned(
        self,
        data: dict[str, Any],
        agent_instance_id: str,
        share_group_id: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def install(
        self,
        *,
        agent_instance_id: str,
        share_group_id: str,
        mode: str = "enforce",
    ) -> dict[str, Any]:
        capability = self.capability()
        if not capability.supported:
            return {
                "provider": self.provider,
                "supported": False,
                "configured": False,
                "status": "unsupported",
                "runtime_verified": False,
                "capability": capability.to_dict(),
            }
        _validate_binding(self.workspace, agent_instance_id, share_group_id)
        path = self.config_path()
        data = _load_json_config(path, strict=True)
        data = self._remove_owned(data)
        data = self._add_owned(data, agent_instance_id, share_group_id)
        _write_json_config(path, data)
        set_hook_mode(
            self.workspace,
            self.provider,
            agent_instance_id,
            mode,
        )
        result = self.status(agent_instance_id=agent_instance_id)
        result["restart_required"] = capability.requires_restart
        result["trust_required"] = capability.requires_trust
        return result

    def uninstall(self) -> dict[str, Any]:
        capability = self.capability()
        if not capability.supported:
            return {
                "provider": self.provider,
                "supported": False,
                "configured": False,
                "status": "unsupported",
                "runtime_verified": False,
                "capability": capability.to_dict(),
            }
        path = self.config_path()
        data = _load_json_config(path, strict=True)
        data = self._remove_owned(data)
        _write_json_config(path, data)
        return {
            "provider": self.provider,
            "supported": True,
            "configured": False,
            "status": "not_configured",
            "runtime_verified": False,
            "config_file": str(path),
            "capability": capability.to_dict(),
        }

    def status(self, *, agent_instance_id: str = "") -> dict[str, Any]:
        capability = self.capability()
        path = self.config_path()
        if not capability.supported:
            return {
                "provider": self.provider,
                "supported": False,
                "configured": False,
                "status": "unsupported",
                "runtime_verified": False,
                "config_file": str(path),
                "capability": capability.to_dict(),
            }
        data = _load_json_config(path, strict=False)
        owned_events = self._owned_events(data)
        expected_events = set(capability.events)
        configured = bool(expected_events) and expected_events.issubset(
            owned_events
        )
        partial = bool(owned_events) and not configured
        resolved_agent_id = agent_instance_id
        if not resolved_agent_id:
            configured_agent_ids = _owned_agent_ids(data)
            if len(configured_agent_ids) == 1:
                resolved_agent_id = next(iter(configured_agent_ids))
        heartbeat = _read_heartbeat(
            self.workspace, self.provider, resolved_agent_id,
        ) if resolved_agent_id else {}
        runtime_verified = configured and _heartbeat_is_current(
            heartbeat,
            _owned_hook_hash(data),
        )
        if runtime_verified:
            status = "operational"
        elif configured:
            status = "configured_pending_runtime"
        elif partial:
            status = "drifted"
        else:
            status = "not_configured"
        return {
            "provider": self.provider,
            "supported": True,
            "configured": configured,
            "installed": configured,
            "drifted": partial,
            "status": status,
            "runtime_verified": runtime_verified,
            "config_file": str(path),
            "last_seen_at": heartbeat.get("at"),
            "last_event": heartbeat.get("event"),
            "last_error": heartbeat.get("error"),
            "agent_instance_id": resolved_agent_id,
            "mode": (
                get_hook_mode(self.workspace, self.provider, resolved_agent_id)
                if resolved_agent_id else "unknown"
            ),
            "capability": capability.to_dict(),
        }

    def _owned_events(self, data: dict[str, Any]) -> set[str]:
        hooks = data.get("hooks", {})
        if not isinstance(hooks, dict):
            return set()
        owned: set[str] = set()
        for event_name, entries in hooks.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if _is_our_handler(entry):
                    owned.add(str(event_name))
                    break
                if isinstance(entry, dict):
                    handlers = entry.get("hooks", [])
                    if isinstance(handlers, list) and any(
                        _is_our_handler(handler) for handler in handlers
                    ):
                        owned.add(str(event_name))
                        break
        return owned


class _NestedJsonHookAdapter(HostHookAdapter):
    """Claude/Codex hooks.json/settings.json nested event shape."""

    def _event_name(self, event: str) -> str:
        return _EVENT_NAMES[event]

    def _remove_owned(self, data: dict[str, Any]) -> dict[str, Any]:
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return data
        for event_name in list(hooks):
            groups = hooks.get(event_name)
            if not isinstance(groups, list):
                continue
            kept_groups: list[Any] = []
            for group in groups:
                if not isinstance(group, dict):
                    kept_groups.append(group)
                    continue
                handlers = group.get("hooks")
                if not isinstance(handlers, list):
                    kept_groups.append(group)
                    continue
                kept_handlers = [
                    handler for handler in handlers
                    if not _is_our_handler(handler)
                ]
                if kept_handlers:
                    copied = dict(group)
                    copied["hooks"] = kept_handlers
                    kept_groups.append(copied)
            if kept_groups:
                hooks[event_name] = kept_groups
            else:
                hooks.pop(event_name, None)
        if not hooks:
            data.pop("hooks", None)
        return data

    def _add_owned(
        self,
        data: dict[str, Any],
        agent_instance_id: str,
        share_group_id: str,
    ) -> dict[str, Any]:
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("invalid hook config: 'hooks' must be an object")
        for event in (
            "session_start",
            "subagent_start",
            "user_prompt",
            "pre_tool",
            "post_tool",
            "pre_compact",
            "stop",
        ):
            handler: dict[str, Any] = {
                "type": "command",
                "command": _command(
                    self.provider,
                    event,
                    self.workspace,
                    agent_instance_id,
                    share_group_id,
                    windows=False,
                ),
                "timeout": 15,
            }
            if self.provider == "codex":
                handler["commandWindows"] = _command(
                    self.provider,
                    event,
                    self.workspace,
                    agent_instance_id,
                    share_group_id,
                    windows=True,
                )
            if (
                self.provider == "codex"
                and event in {
                    "session_start",
                    "subagent_start",
                    "user_prompt",
                }
            ):
                handler["additionalContextLimit"] = 1800
            hooks.setdefault(self._event_name(event), []).append({
                "hooks": [handler],
            })
        return data


class ClaudeHookAdapter(_NestedJsonHookAdapter):
    provider = "claude"

    def config_path(self) -> Path:
        configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
        root = Path(configured).expanduser() if configured else Path.home() / ".claude"
        return root / "settings.json"

    def capability(self) -> HookCapability:
        return HookCapability(
            provider=self.provider,
            supported=True,
            config_file=str(self.config_path()),
            events=tuple(_EVENT_NAMES.values()),
            context_mode="per_turn_additional_context",
            native_write_guard="pre_tool_deny_visible_writes",
            stop_guard="single_continuation_for_durable_candidate",
            requires_restart=True,
            requires_trust=False,
            notes=("user settings apply to all projects and subagents",),
        )


class CodexHookAdapter(_NestedJsonHookAdapter):
    provider = "codex"

    def config_path(self) -> Path:
        configured = os.environ.get("CODEX_HOME", "").strip()
        root = Path(configured).expanduser() if configured else Path.home() / ".codex"
        return root / "hooks.json"

    def capability(self) -> HookCapability:
        return HookCapability(
            provider=self.provider,
            supported=True,
            config_file=str(self.config_path()),
            events=tuple(_EVENT_NAMES.values()),
            context_mode="per_turn_additional_context",
            native_write_guard="pre_tool_deny_visible_writes",
            stop_guard="single_continuation_for_durable_candidate",
            requires_restart=True,
            requires_trust=True,
            notes=("open /hooks once after install to trust the exact hook hash",),
        )


class CursorHookAdapter(HostHookAdapter):
    provider = "cursor"

    def config_path(self) -> Path:
        return Path.home() / ".cursor" / "hooks.json"

    def capability(self) -> HookCapability:
        return HookCapability(
            provider=self.provider,
            supported=True,
            config_file=str(self.config_path()),
            events=tuple(_CURSOR_EVENT_NAMES.values()),
            context_mode="session_context_plus_first_tool_bootstrap",
            native_write_guard="pre_tool_deny_visible_writes",
            stop_guard="single_followup_for_durable_candidate",
            requires_restart=False,
            requires_trust=False,
            notes=(
                "beforeSubmitPrompt cannot inject dynamic context",
                "pure no-tool replies rely on the alwaysApply MemoryGuard rule",
            ),
        )

    def _remove_owned(self, data: dict[str, Any]) -> dict[str, Any]:
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return data
        for event_name in list(hooks):
            entries = hooks.get(event_name)
            if not isinstance(entries, list):
                continue
            kept = [entry for entry in entries if not _is_our_handler(entry)]
            if kept:
                hooks[event_name] = kept
            else:
                hooks.pop(event_name, None)
        if not hooks:
            data.pop("hooks", None)
        return data

    def _add_owned(
        self,
        data: dict[str, Any],
        agent_instance_id: str,
        share_group_id: str,
    ) -> dict[str, Any]:
        data.setdefault("version", 1)
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("invalid hook config: 'hooks' must be an object")
        for event in (
            "session_start",
            "user_prompt",
            "pre_tool",
            "post_tool",
            "pre_compact",
            "stop",
        ):
            entry: dict[str, Any] = {
                "command": _command(
                    self.provider,
                    event,
                    self.workspace,
                    agent_instance_id,
                    share_group_id,
                ),
                "timeout": 15,
            }
            if event == "pre_tool":
                entry["failClosed"] = True
            if event == "stop":
                entry["loop_limit"] = 1
            hooks.setdefault(_CURSOR_EVENT_NAMES[event], []).append(entry)
        return data


class TraeHookAdapter(HostHookAdapter):
    provider = "trae"

    def config_path(self) -> Path:
        return Path.home() / ".trae" / "hooks.json"

    def capability(self) -> HookCapability:
        return HookCapability(
            provider=self.provider,
            supported=False,
            config_file="",
            events=(),
            context_mode="mcp_and_rules_only",
            native_write_guard="unavailable",
            stop_guard="unavailable",
            requires_restart=False,
            requires_trust=False,
            notes=("no verified official user-level lifecycle hook seam",),
        )

    def _remove_owned(self, data: dict[str, Any]) -> dict[str, Any]:
        return data

    def _add_owned(
        self,
        data: dict[str, Any],
        agent_instance_id: str,
        share_group_id: str,
    ) -> dict[str, Any]:
        return data


HOOK_ADAPTERS: dict[str, type[HostHookAdapter]] = {
    "claude": ClaudeHookAdapter,
    "claude-code": ClaudeHookAdapter,
    "codex": CodexHookAdapter,
    "cursor": CursorHookAdapter,
    "trae": TraeHookAdapter,
}


def _current_hook_hash(workspace: Path, provider: str) -> str:
    adapter_cls = HOOK_ADAPTERS.get(provider)
    if adapter_cls is None:
        return ""
    adapter = adapter_cls(workspace)
    data = _load_json_config(adapter.config_path(), strict=False)
    return _owned_hook_hash(data)


class HostHookManager:
    """Deep module: install, remove, inspect, and execute every host hook."""

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).expanduser().resolve()

    def adapter(self, provider: str) -> HostHookAdapter:
        normalized = (provider or "").strip().lower()
        adapter_cls = HOOK_ADAPTERS.get(normalized)
        if adapter_cls is None:
            raise ValueError(
                f"unknown hook provider {provider!r}; "
                "supported: claude|codex|cursor|trae"
            )
        return adapter_cls(self.workspace)

    def install(
        self,
        provider: str,
        *,
        agent_instance_id: str,
        share_group_id: str,
        mode: str = "enforce",
    ) -> dict[str, Any]:
        return self.adapter(provider).install(
            agent_instance_id=agent_instance_id,
            share_group_id=share_group_id,
            mode=mode,
        )

    def uninstall(self, provider: str) -> dict[str, Any]:
        return self.adapter(provider).uninstall()

    def status(
        self,
        provider: str = "",
        *,
        agent_instance_id: str = "",
    ) -> dict[str, Any]:
        if provider:
            return self.adapter(provider).status(
                agent_instance_id=agent_instance_id,
            )
        statuses = [
            adapter_cls(self.workspace).status(
                agent_instance_id=agent_instance_id,
            )
            for adapter_cls in (
                ClaudeHookAdapter,
                CodexHookAdapter,
                CursorHookAdapter,
                TraeHookAdapter,
            )
        ]
        return {
            "providers": statuses,
            "configured_count": sum(
                bool(item.get("configured")) for item in statuses
            ),
            "operational_count": sum(
                bool(item.get("runtime_verified")) for item in statuses
            ),
        }


def _policy_path(workspace: Path) -> Path:
    return workspace / ".memoryguard" / _RUNTIME_DIR / "policy.json"


def set_hook_mode(
    workspace: str | Path,
    provider: str,
    agent_instance_id: str,
    mode: str,
) -> dict[str, Any]:
    normalized = (mode or "").strip().lower()
    if normalized not in HOOK_MODES:
        raise ValueError(f"invalid hook mode: {mode!r}")
    root = Path(workspace).expanduser().resolve()
    path = _policy_path(root)
    data = _load_json_config(path, strict=True)
    key = f"{provider.lower()}:{agent_instance_id}"
    data[key] = {
        "mode": normalized,
        "updated_at": _now_iso(),
    }
    _write_json_config(path, data)
    return {
        "provider": provider.lower(),
        "agent_instance_id": agent_instance_id,
        "mode": normalized,
    }


def get_hook_mode(
    workspace: str | Path,
    provider: str,
    agent_instance_id: str,
) -> str:
    root = Path(workspace).expanduser().resolve()
    data = _load_json_config(_policy_path(root), strict=False)
    item = data.get(f"{provider.lower()}:{agent_instance_id}", {})
    mode = str(item.get("mode", "enforce") or "enforce").lower()
    return mode if mode in HOOK_MODES else "enforce"


def _runtime_root(workspace: Path) -> Path:
    return workspace / ".memoryguard" / _RUNTIME_DIR


def _state_path(workspace: Path, provider: str, session_id: str) -> Path:
    return (
        _runtime_root(workspace)
        / "state"
        / provider
        / f"{_short_hash(session_id)}.json"
    )


def _heartbeat_path(
    workspace: Path,
    provider: str,
    agent_instance_id: str,
) -> Path:
    return (
        _runtime_root(workspace)
        / "heartbeat"
        / f"{provider}-{_short_hash(agent_instance_id)}.json"
    )


def _load_state(workspace: Path, provider: str, session_id: str) -> dict[str, Any]:
    if not session_id:
        return {}
    return _load_json_config(
        _state_path(workspace, provider, session_id),
        strict=False,
    )


def _save_state(
    workspace: Path,
    provider: str,
    session_id: str,
    state: dict[str, Any],
) -> None:
    if not session_id:
        return
    state = dict(state)
    state["updated_at"] = _now_iso()
    try:
        _write_json_config(_state_path(workspace, provider, session_id), state)
    except Exception as exc:
        _emit_runtime_write_diagnostic("mandatory_state_write_failed", provider, "state", exc)
        raise


def _record_heartbeat(
    workspace: Path,
    provider: str,
    agent_instance_id: str,
    *,
    event: str,
    error: str = "",
    mandatory_rule_ids: list[str] | None = None,
    mandatory_overflow: bool = False,
) -> bool:
    path = _heartbeat_path(workspace, provider, agent_instance_id)
    try:
        previous = _load_json_config(path, strict=False)
        payload = {
            "provider": provider,
            "agent_hash": _short_hash(agent_instance_id),
            "hook_version": HOOK_VERSION,
            "hook_hash": _current_hook_hash(workspace, provider),
            "event": event,
            "at": _now_iso(),
            "error": (error or "")[:500],
            "mandatory_rule_ids": list(mandatory_rule_ids or []),
            "mandatory_overflow": bool(mandatory_overflow),
        }
        if isinstance(previous.get("history_archive"), dict):
            payload["history_archive"] = previous["history_archive"]
        _write_json_config(path, payload)
        return True
    except Exception as exc:
        _emit_runtime_write_diagnostic("heartbeat_write_failed", provider, event, exc)
        return False


def _emit_runtime_write_diagnostic(kind: str, provider: str, event: str, exc: Exception) -> None:
    print(json.dumps({
        "memoryguard_hook_diagnostic": kind,
        "provider": provider,
        "event": event,
        "error_type": type(exc).__name__,
    }, ensure_ascii=True), file=sys.stderr)


def _record_history_diagnostic(
    workspace: Path,
    provider: str,
    agent_instance_id: str,
    diagnostic: dict[str, Any],
) -> bool:
    """Attach non-content history coverage data to the hook receipt.

    Runtime receipts are intentionally safe to inspect.  They contain IDs,
    hashes, limits and failure categories only -- never the prompt or model
    response that was archived in the separate history SQLite database.
    """
    path = _heartbeat_path(workspace, provider, agent_instance_id)
    try:
        receipt = _load_json_config(path, strict=False)
        receipt["history_archive"] = dict(diagnostic)
        receipt["at"] = _now_iso()
        _write_json_config(path, receipt)
        return True
    except Exception as exc:
        _emit_runtime_write_diagnostic("history_receipt_write_failed", provider, "history", exc)
        return False


def _read_heartbeat(
    workspace: Path,
    provider: str,
    agent_instance_id: str,
) -> dict[str, Any]:
    if not agent_instance_id:
        return {}
    return _load_json_config(
        _heartbeat_path(workspace, provider, agent_instance_id),
        strict=False,
    )


def _heartbeat_is_current(
    heartbeat: dict[str, Any],
    hook_hash: str,
) -> bool:
    if not heartbeat or heartbeat.get("error"):
        return False
    if str(heartbeat.get("hook_version", "")) != HOOK_VERSION:
        return False
    if not hook_hash or str(heartbeat.get("hook_hash", "")) != hook_hash:
        return False
    try:
        timestamp = datetime.fromisoformat(str(heartbeat["at"]))
        age = datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)
        return age.days <= _MAX_RECEIPT_AGE_DAYS
    except (KeyError, TypeError, ValueError):
        return False


def _session_id(payload: dict[str, Any]) -> str:
    base = str(
        payload.get("session_id")
        or payload.get("conversation_id")
        or "unknown-session"
    )
    subagent = str(
        payload.get("subagent_id")
        or payload.get("agent_id")
        or ""
    )
    return f"{base}:subagent:{subagent}" if subagent else base


def _prompt(payload: dict[str, Any]) -> str:
    return str(
        payload.get("prompt")
        or payload.get("user_prompt")
        or payload.get("initial_prompt")
        or ""
    )


_HISTORY_PRIVATE_KEYS = (
    "private", "sensitive", "do_not_archive", "history_disabled",
    "memoryguard_history_disabled", "incognito",
)


def _history_opted_out(payload: dict[str, Any]) -> bool:
    """Honor only explicit host-supplied privacy/disable indicators."""
    containers = [payload]
    for key in ("metadata", "context", "conversation"):
        value = payload.get(key)
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        for key in _HISTORY_PRIVATE_KEYS:
            value = container.get(key)
            if value is True or str(value).strip().casefold() in {"1", "true", "yes", "on"}:
                return True
    return False


def _history_capture_enabled(workspace: Path) -> tuple[bool, str]:
    env_value = os.environ.get("MEMORYGUARD_HISTORY_ENABLED", "").strip().casefold()
    if env_value in {"0", "false", "no", "off"}:
        return False, "disabled_by_env"
    config = _load_json_config(
        workspace / ".memoryguard" / "history" / "config.json",
        strict=False,
    )
    if config.get("enabled") is False:
        return False, "disabled_by_config"
    return True, "enabled"


def _stable_history_event_id(payload: dict[str, Any], event: str) -> tuple[str, bool, str]:
    for key in ("turn_id", "message_id", "generation_id", "event_id"):
        value = payload.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return f"{event}:id:{key}:{str(value).strip()}", True, key
    # Some verified seams expose a monotonic sequence/offset but no opaque ID.
    for key in ("sequence", "sequence_id", "turn_index", "message_index", "offset"):
        value = payload.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return f"{event}:sequence:{key}:{str(value).strip()}", True, key
    return "", False, "unavailable"


def _contains_obvious_secret(content: str) -> bool:
    from .auto_organizer import SECRET_PATTERNS
    return any(pattern.search(content) for pattern in SECRET_PATTERNS)


def _hook_history_session_id(payload: dict[str, Any]) -> str:
    """Use a verified host session identity; never coalesce unknown chats."""
    base = str(payload.get("session_id") or payload.get("conversation_id") or "").strip()
    if not base:
        return ""
    subagent = str(payload.get("subagent_id") or payload.get("agent_id") or "").strip()
    return f"{base}:subagent:{subagent}" if subagent else base


def _archive_history_event(
    *,
    workspace: Path,
    provider: str,
    event: str,
    agent_instance_id: str,
    share_group_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Best-effort raw turn archive for verified lifecycle seams.

    This never calls the long-term-memory store or bootstrap. Strict replay
    idempotency is promised only when the host supplies a stable event ID or
    sequence. Otherwise each observed call is preserved and coverage is marked
    degraded. Failures never affect the host's normal conversation flow.
    """
    if event not in {"user_prompt", "stop"}:
        return {"attempted": False, "reason": "event_not_archived"}
    enabled, enabled_reason = _history_capture_enabled(workspace)
    if not enabled:
        return {"attempted": False, "reason": enabled_reason, "capture_enabled": False}
    if _history_opted_out(payload):
        return {"attempted": False, "reason": "private_or_disabled"}
    external_session_id = _hook_history_session_id(payload)
    if not external_session_id:
        return {"attempted": False, "reason": "session_identity_missing"}
    if event == "user_prompt":
        role, content = "user", _prompt(payload)
    else:
        role = "assistant"
        # A Stop event does not universally expose model text.  Do not invent
        # it, scrape state, or claim full host coverage when it is absent.
        content = str(
            payload.get("last_assistant_message")
            or payload.get("assistant_message")
            or payload.get("final_response")
            or ""
        )
    if not content.strip():
        return {
            "attempted": False,
            "reason": "assistant_content_unavailable" if event == "stop" else "prompt_content_unavailable",
            "session_hash": _short_hash(external_session_id),
        }
    if _contains_obvious_secret(content):
        return {
            "attempted": False, "archived": False,
            "reason": "secret_detected_blocked", "secret_blocked": True,
            "session_hash": _short_hash(external_session_id),
        }
    try:
        from .conversation_history import ConversationHistoryStore, HistoryScope, MAX_TURN_CHARS

        truncated = len(content) > MAX_TURN_CHARS
        if truncated:
            content = content[:MAX_TURN_CHARS]
        host_event_id, event_stable, event_source = _stable_history_event_id(payload, event)
        result = ConversationHistoryStore(workspace).append_turn(
            HistoryScope(
                agent_instance_id=agent_instance_id,
                project_ref=str(payload.get("project_ref") or payload.get("cwd") or ""),
                provider=provider,
                share_group_id=share_group_id,
            ),
            external_session_id=external_session_id,
            provider=provider,
            role=role,
            content=content,
            event_id=host_event_id,
            event_stable=event_stable,
            title=str(payload.get("title") or payload.get("conversation_title") or ""),
            created_at=str(payload.get("timestamp") or payload.get("created_at") or ""),
        )
        return {
            "attempted": True,
            "archived": bool(result.get("inserted")),
            "replayed": bool(result.get("replayed")),
            "event_conflict": bool(result.get("event_conflict")),
            "event": event,
            "role": role,
            "session_hash": _short_hash(external_session_id),
            "truncated": truncated,
            "capture_enabled": True,
            "idempotency": result.get("idempotency", "degraded"),
            "coverage_degraded": not event_stable,
            "event_identity_source": event_source,
        }
    except Exception as exc:
        return {
            "attempted": True,
            "archived": False,
            "event": event,
            "reason": f"history_archive_failed:{type(exc).__name__}",
            "session_hash": _short_hash(external_session_id),
        }


def _durable_candidate(text: str) -> bool:
    value = (text or "").strip()
    return bool(value) and any(pattern.search(value) for pattern in _DURABLE_PATTERNS)


def _flatten_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(_flatten_strings(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_flatten_strings(item))
        return result
    return []


def _normalized_strings(value: Any) -> list[str]:
    return [
        item.replace("\\", "/").casefold()
        for item in _flatten_strings(value)
        if item
    ]


def _targets_native_memory(tool_name: str, tool_input: Any) -> bool:
    normalized_tool = (tool_name or "").casefold()
    strings = _normalized_strings(tool_input)
    if not any(
        pattern.search(value)
        for value in strings
        for pattern in _NATIVE_PATH_PATTERNS
    ):
        return False
    if any(
        token in normalized_tool
        for token in ("write", "edit", "delete", "apply_patch")
    ):
        return True
    if normalized_tool in {"bash", "shell"}:
        return any(_SHELL_WRITE_PATTERN.search(value) for value in strings)
    return False


def _is_memoryguard_tool(tool_name: str, operation: str = "") -> bool:
    value = (tool_name or "").casefold()
    return "memoryguard" in value and operation.casefold() in value


def _cursor_mcp_inner_tool_name(tool_name: str, tool_input: Any = None) -> str:
    """Cursor agents wrap MCP as CallMcpTool; real MCP tool name is in tool_input."""
    raw = (tool_name or "").casefold()
    compact = raw.replace("_", "")
    if compact != "callmcptool" and "callmcptool" not in compact:
        return ""
    if not isinstance(tool_input, dict):
        return ""
    inner = tool_input.get("toolName") or tool_input.get("tool_name") or ""
    return str(inner)


def _is_memoryguard_bootstrap(tool_name: str, tool_input: Any = None) -> bool:
    if _is_memoryguard_tool(tool_name, "context_bootstrap"):
        return True
    inner = _cursor_mcp_inner_tool_name(tool_name, tool_input)
    return bool(inner) and _is_memoryguard_tool(inner, "context_bootstrap")


def _is_memoryguard_write(tool_name: str, tool_input: Any = None) -> bool:
    if _is_memoryguard_tool(tool_name, "memory_write"):
        return True
    inner = _cursor_mcp_inner_tool_name(tool_name, tool_input)
    return bool(inner) and _is_memoryguard_tool(inner, "memory_write")


def _is_other_memory_write(tool_name: str) -> bool:
    value = (tool_name or "").casefold()
    if "memoryguard" in value or "memory" not in value:
        return False
    is_mcp = value.startswith("mcp__") or value.startswith("mcp:")
    is_write = any(
        token in value
        for token in ("write", "add", "create", "update", "delete", "store", "remember")
    )
    return is_mcp and is_write


def _load_store(
    workspace: Path,
    agent_instance_id: str,
    share_group_id: str,
):
    _validate_binding(workspace, agent_instance_id, share_group_id)
    from .shared_memory_store import SharedMemoryStore

    return SharedMemoryStore(workspace, share_group_id)


def _render_context(packet: dict[str, Any]) -> str:
    context_packet = packet.get("context_packet", {})
    items = context_packet.get("items", [])
    mandatory_items = context_packet.get("mandatory_items", [])
    if packet.get("mandatory_overflow"):
        return (
            "MemoryGuard 强制规则包异常，停止继续执行。"
            f"原因：{packet.get('error') or 'mandatory_rule_package_invalid'}"
        )
    lines = [
        "[MemoryGuard 强制规则（必须遵循）]",
    ]
    if mandatory_items:
        for item in mandatory_items:
            body = " ".join(str(item.get("body", "")).split())
            if body:
                lines.append(f"- {item.get('kind', 'fact')}: {body}")
    else:
        lines.append("- 本轮没有生效的强制规则。")
    lines.extend([
        "[MemoryGuard 相关长期记忆]",
        "仅用于补充长期规则/偏好/项目决策；当前宿主对话保持原样。",
    ])
    for item in items:
        body = " ".join(str(item.get("body", "")).split())
        if not body:
            continue
        lines.append(f"- {item.get('kind', 'fact')}: {body}")
    if len(lines) == 2:
        lines.append("- 本轮没有匹配的长期记忆。")
    lines.append(
        "长期记忆写入只使用 memoryguard_memory_write；"
        "不得写入宿主原生记忆文件。"
    )
    return "\n".join(lines)


def _static_session_context(provider: str) -> str:
    base = (
        "MemoryGuard Hook 已启用。长期记忆与长期规则的唯一真相源是 "
        "MemoryGuard；当前对话上下文仍由宿主管理，不复制为长期记忆。"
    )
    if provider == "cursor":
        return (
            base
            + " Cursor 当前不能在 beforeSubmitPrompt 动态注入上下文；"
            "首次工具调用前必须先调用 memoryguard_context_bootstrap。"
        )
    return base


def _context_output(provider: str, event: str, text: str) -> dict[str, Any]:
    if provider == "cursor":
        if event == "session_start":
            return {"additional_context": text}
        if event == "user_prompt":
            return {"continue": True}
        if event == "pre_compact":
            return {"user_message": text}
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": _EVENT_NAMES[event],
            "additionalContext": text,
        }
    }


def _normalize_feedback_receipts(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    items: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        receipt_id = str(item.get("receipt_id", "")).strip()
        if not receipt_id:
            continue
        normalized = {
            "receipt_id": receipt_id,
            "memory_id": str(item.get("memory_id", "") or "").strip(),
        }
        items.append(normalized)
    return items


def _persist_mandatory_match_receipts(
    *,
    store: "SharedMemoryStore",
    receipts: list[dict[str, Any]],
    provider: str,
    event: str,
) -> None:
    if not receipts:
        return
    from .schema_v3 import RuleMatchReceipt

    for raw in receipts:
        if not isinstance(raw, dict):
            continue
        try:
            store.append_rule_match_receipt(RuleMatchReceipt.from_dict(raw))
        except Exception as exc:
            _emit_runtime_write_diagnostic(
                "mandatory_receipt_write_failed",
                provider,
                event,
                exc,
            )


def _flush_pending_rule_feedback(
    *,
    workspace: Path,
    provider: str,
    agent_instance_id: str,
    share_group_id: str,
    session_id: str,
    actor: str,
    trigger: str,
) -> None:
    state = _load_state(workspace, provider, session_id)
    receipts = _normalize_feedback_receipts(state.get("mandatory_match_receipts", []))
    if not receipts:
        return

    try:
        store = _load_store(workspace, agent_instance_id, share_group_id)
    except Exception as exc:
        _emit_runtime_write_diagnostic(
            "rule_feedback_fallback_store_open_failed",
            provider,
            "stop",
            exc,
        )
        return

    from .schema_v3 import RuleMatchFeedback, stable_hash

    for raw in receipts:
        receipt_id = raw["receipt_id"]
        try:
            existing = store.get_rule_match_feedback_by_receipt(receipt_id)
        except Exception as exc:
            _emit_runtime_write_diagnostic(
                "rule_feedback_fallback_lookup_failed",
                provider,
                "stop",
                exc,
            )
            continue
        if existing is not None:
            continue
        feedback = RuleMatchFeedback(
            feedback_id=stable_hash(
                "rule-feedback", "not_applicable", receipt_id, actor, trigger,
            ),
            receipt_id=receipt_id,
            outcome="not_applicable",
            actor=actor,
            evidence=f"auto fallback from stop: {trigger}",
            confidence=1.0,
            created_at=_now_iso(),
        )
        try:
            store.append_rule_match_feedback(feedback)
        except Exception as exc:
            _emit_runtime_write_diagnostic(
                "rule_feedback_fallback_write_failed",
                provider,
                "stop",
                exc,
            )
            continue

    state["mandatory_match_receipts"] = []
    _save_state(workspace, provider, session_id, state)


def _deny_output(provider: str, reason: str) -> dict[str, Any]:
    if provider == "cursor":
        return {
            "permission": "deny",
            "user_message": reason,
            "agent_message": reason,
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _stop_continue_output(provider: str, reason: str) -> dict[str, Any]:
    if provider == "cursor":
        return {"followup_message": reason}
    return {"decision": "block", "reason": reason}


def _allow_output(provider: str, event: str) -> dict[str, Any]:
    if provider == "cursor" and event == "user_prompt":
        return {"continue": True}
    return {}


def run_hook(
    *,
    provider: str,
    event: str,
    workspace: str | Path,
    agent_instance_id: str,
    share_group_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Run one host hook.  Returned data is the host's stdout JSON."""
    normalized_provider = provider.strip().lower()
    if normalized_provider == "claude-code":
        normalized_provider = "claude"
    if normalized_provider not in {"claude", "codex", "cursor"}:
        return {}
    if event not in _EVENT_NAMES:
        raise ValueError(f"unknown hook event: {event}")

    root = Path(workspace).expanduser().resolve()
    session_id = _session_id(payload)
    mode = get_hook_mode(root, normalized_provider, agent_instance_id)
    _record_heartbeat(
        root,
        normalized_provider,
        agent_instance_id,
        event=event,
    )
    if mode == "paused":
        return _allow_output(normalized_provider, event)

    # The three installed adapters all use these verified lifecycle seams.
    # Archive only event payload supplied by that host; this is best-effort and
    # deliberately independent from long-term memory/bootstrapping.
    if event in {"user_prompt", "stop"}:
        _record_history_diagnostic(
            root,
            normalized_provider,
            agent_instance_id,
            _archive_history_event(
                workspace=root,
                provider=normalized_provider,
                event=event,
                agent_instance_id=agent_instance_id,
                share_group_id=share_group_id,
                payload=payload,
            ),
        )

    if event == "session_start":
        text = _static_session_context(normalized_provider)
        if normalized_provider == "cursor":
            try:
                store = _load_store(root, agent_instance_id, share_group_id)
                from .context_bootstrap import build_context_packet

                packet = build_context_packet(
                    store,
                    task="MemoryGuard session preferences",
                    max_items=5,
                    max_chars=2400,
                    effective_context=_effective_agent_context(
                        normalized_provider, agent_instance_id,
                        share_group_id, payload, event=event,
                    ),
                )
                _record_heartbeat(
                    root, normalized_provider, agent_instance_id, event=event,
                    error=str(packet.get("error", "")),
                    mandatory_rule_ids=packet.get("mandatory_rule_ids", []),
                    mandatory_overflow=bool(packet.get("mandatory_overflow")),
                )
                _save_state(root, normalized_provider, session_id, {
                    "mandatory_rule_ids": packet.get("mandatory_rule_ids", []),
                    "mandatory_overflow": bool(packet.get("mandatory_overflow")),
                    "mandatory_invalid_reason": packet.get("mandatory_invalid_reason", ""),
                    "mandatory_match_receipts": packet.get(
                        "mandatory_match_receipts", [],
                    ),
                    "bootstrap_ok": not bool(packet.get("mandatory_overflow")),
                })
                _persist_mandatory_match_receipts(
                    store=store,
                    receipts=packet.get("mandatory_match_receipts", []),
                    provider=normalized_provider,
                    event="session_start",
                )
                text = text + "\n" + _render_context(packet)
            except Exception as exc:
                _save_state(root, normalized_provider, session_id, {
                    "mandatory_overflow": True,
                    "mandatory_invalid_reason": f"context bootstrap failed: {exc}",
                    "mandatory_match_receipts": [],
                    "bootstrap_ok": False,
                })
                _record_heartbeat(
                    root,
                    normalized_provider,
                    agent_instance_id,
                    event=event,
                    error=f"context bootstrap failed: {exc}",
                )
        elif (
            normalized_provider == "codex"
            and str(payload.get("source", "") or "").casefold() == "compact"
        ):
            state = _load_state(root, normalized_provider, session_id)
            if state.get("durable_candidate") and not state.get("write_seen"):
                text = text + "\n" + _COMPACT_REMINDER
        return _context_output(normalized_provider, event, text)

    if event == "subagent_start":
        task = str(
            payload.get("task")
            or payload.get("prompt")
            or payload.get("agent_prompt")
            or "subagent task"
        )
        try:
            store = _load_store(root, agent_instance_id, share_group_id)
            from .context_bootstrap import build_context_packet

            packet = build_context_packet(
                store,
                task=task,
                project_hint=str(payload.get("cwd", "") or ""),
                max_items=6,
                max_chars=3000,
                effective_context=_effective_agent_context(
                    normalized_provider, agent_instance_id,
                    share_group_id, payload, event=event,
                ),
            )
            effective = packet.get("effective_agent", {})
            receipt = packet.get("assignment_receipt", {})
            _save_state(root, normalized_provider, session_id, {
                "bootstrap_ok": not bool(packet.get("mandatory_overflow")),
                "mandatory_overflow": bool(packet.get("mandatory_overflow")),
                "mandatory_invalid_reason": packet.get(
                    "mandatory_invalid_reason", ""
                ),
                "mandatory_rule_ids": packet.get("mandatory_rule_ids", []),
                "mandatory_match_receipts": packet.get(
                    "mandatory_match_receipts", [],
                ),
                "effective_agent": effective,
                "assignment_receipt": receipt,
            })
            _persist_mandatory_match_receipts(
                store=store,
                receipts=packet.get("mandatory_match_receipts", []),
                provider=normalized_provider,
                event="subagent_start",
            )
            _record_heartbeat(
                root, normalized_provider, agent_instance_id, event=event,
                error=str(packet.get("error", "")),
                mandatory_rule_ids=packet.get("mandatory_rule_ids", []),
                mandatory_overflow=bool(packet.get("mandatory_overflow")),
            )
            return _context_output(
                normalized_provider,
                event,
                _static_session_context(normalized_provider)
                + "\n"
                + _render_context(packet),
            )
        except Exception as exc:
            state = _load_state(root, normalized_provider, session_id)
            state.update({
                "mandatory_overflow": True,
                "mandatory_invalid_reason": f"context bootstrap failed: {exc}",
                "mandatory_match_receipts": [],
                "bootstrap_ok": False,
            })
            _save_state(root, normalized_provider, session_id, state)
            _record_heartbeat(
                root,
                normalized_provider,
                agent_instance_id,
                event=event,
                error=f"subagent context bootstrap failed: {exc}",
            )
            return _context_output(
                normalized_provider,
                event,
                "MemoryGuard 子代理上下文加载失败；不得写入宿主原生记忆。",
            )

    if event == "user_prompt":
        prompt = _prompt(payload)
        previous_state = _load_state(root, normalized_provider, session_id)
        state = {
            "prompt_hash": _short_hash(prompt),
            "durable_candidate": _durable_candidate(prompt),
            "bootstrap_ok": False,
            "write_seen": False,
            "stop_continued": False,
        }
        if normalized_provider == "cursor":
            try:
                store = _load_store(root, agent_instance_id, share_group_id)
                from .context_bootstrap import build_context_packet
                packet = build_context_packet(
                    store, task="MemoryGuard session mandatory verification",
                    max_items=5, max_chars=2400,
                    effective_context=_effective_agent_context(
                        normalized_provider, agent_instance_id,
                        share_group_id, payload, event=event,
                    ),
                )
                state.update({
                    "mandatory_rule_ids": packet.get("mandatory_rule_ids", previous_state.get("mandatory_rule_ids", [])),
                    "mandatory_overflow": bool(packet.get("mandatory_overflow")),
                    "mandatory_invalid_reason": packet.get("mandatory_invalid_reason", ""),
                    "mandatory_match_receipts": packet.get(
                        "mandatory_match_receipts", [],
                    ),
                })
                _persist_mandatory_match_receipts(
                    store=store,
                    receipts=packet.get("mandatory_match_receipts", []),
                    provider=normalized_provider,
                    event="user_prompt",
                )
            except Exception as exc:
                state.update({
                    "mandatory_overflow": True,
                    "mandatory_invalid_reason": f"context bootstrap failed: {exc}",
                    "mandatory_match_receipts": [],
                })
            _save_state(root, normalized_provider, session_id, state)
            return {"continue": True}
        try:
            store = _load_store(root, agent_instance_id, share_group_id)
            from .context_bootstrap import build_context_packet

            packet = build_context_packet(
                store,
                task=prompt or "current task",
                project_hint=str(payload.get("cwd", "") or ""),
                max_items=8,
                max_chars=4000,
                effective_context=_effective_agent_context(
                    normalized_provider, agent_instance_id,
                    share_group_id, payload, event=event,
                ),
            )
            state["mandatory_rule_ids"] = packet.get("mandatory_rule_ids", [])
            state["mandatory_overflow"] = bool(packet.get("mandatory_overflow"))
            state["mandatory_match_receipts"] = packet.get(
                "mandatory_match_receipts", [],
            )
            state["bootstrap_ok"] = not state["mandatory_overflow"]
            state["selected_count"] = int(
                packet.get("selection", {}).get("selected_count", 0)
            )
            _persist_mandatory_match_receipts(
                store=store,
                receipts=packet.get("mandatory_match_receipts", []),
                provider=normalized_provider,
                event="user_prompt",
            )
            _save_state(root, normalized_provider, session_id, state)
            _record_heartbeat(
                root, normalized_provider, agent_instance_id, event=event,
                error=str(packet.get("error", "")),
                mandatory_rule_ids=state["mandatory_rule_ids"],
                mandatory_overflow=state["mandatory_overflow"],
            )
            return _context_output(
                normalized_provider,
                event,
                _render_context(packet),
            )
        except Exception as exc:
            state["bootstrap_error"] = str(exc)[:500]
            state["mandatory_overflow"] = True
            state["mandatory_invalid_reason"] = state["bootstrap_error"]
            state["mandatory_match_receipts"] = []
            _save_state(root, normalized_provider, session_id, state)
            _record_heartbeat(
                root,
                normalized_provider,
                agent_instance_id,
                event=event,
                error=f"context bootstrap failed: {exc}",
            )
            return _context_output(
                normalized_provider,
                event,
                "MemoryGuard 本轮上下文加载失败；请先修复绑定或运行 "
                "`memoryguard hooks status`。在修复前不得回退写入宿主原生记忆。",
            )

    if event == "pre_tool":
        tool_name = str(payload.get("tool_name", "") or "")
        tool_input = payload.get("tool_input", {})
        state = _load_state(root, normalized_provider, session_id)
        if (state.get("mandatory_overflow") or state.get("bootstrap_error")) and mode == "enforce":
            return _deny_output(
                normalized_provider,
                "MemoryGuard 强制规则包异常，停止继续执行。请先修复共享记忆中的强制规则。",
            )
        if _targets_native_memory(tool_name, tool_input):
            reason = (
                "MemoryGuard 已接管长期记忆：禁止 Agent 写入宿主原生记忆路径。"
                "请改用 memoryguard_memory_write；人工 GUI 删除/恢复不受影响。"
            )
            if mode == "enforce":
                return _deny_output(normalized_provider, reason)
            return {}
        if _is_other_memory_write(tool_name):
            reason = (
                "检测到其他记忆 MCP 写入。正式接管模式只允许 "
                "MemoryGuard 作为长期记忆写入端。"
            )
            if mode == "enforce":
                return _deny_output(normalized_provider, reason)
            return {}
        if normalized_provider == "cursor":
            state = _load_state(root, normalized_provider, session_id)
            is_subagent = bool(
                payload.get("subagent_id") or payload.get("agent_id")
            )
            if not state and is_subagent:
                state = {
                    "durable_candidate": False,
                    "bootstrap_ok": False,
                    "write_seen": False,
                    "stop_continued": False,
                }
                _save_state(root, normalized_provider, session_id, state)
            # Cursor: mark bootstrap on pre_tool; do not rely on postToolUse for MCP.
            if _is_memoryguard_bootstrap(tool_name, tool_input):
                if not isinstance(state, dict):
                    state = {}
                if not state.get("mandatory_overflow"):
                    state["bootstrap_ok"] = True
                    _save_state(root, normalized_provider, session_id, state)
                return {}
            if (
                isinstance(state, dict)
                and state
                and not state.get("bootstrap_ok")
                and mode == "enforce"
            ):
                return _deny_output(
                    normalized_provider,
                    "开始本轮工具操作前，先调用 "
                    "memoryguard_context_bootstrap(task=当前用户请求)。"
                )
        return {}

    if event == "post_tool":
        tool_name = str(payload.get("tool_name", "") or "")
        tool_input = payload.get("tool_input", {})
        tool_result = payload.get("tool_result")
        if tool_result is None:
            tool_result = payload.get("result")
        state = _load_state(root, normalized_provider, session_id)
        changed = False
        if _is_memoryguard_bootstrap(tool_name, tool_input):
            if not state.get("mandatory_overflow"):
                state["bootstrap_ok"] = True
                changed = True
        if _is_memoryguard_write(tool_name, tool_input):
            success, reason = _coerce_tool_result_status(tool_result)
            if success is True:
                state["write_seen"] = True
                state.pop("write_failed", None)
                state.pop("write_error", None)
                changed = True
            elif success is False:
                if not state.get("write_seen"):
                    state["write_seen"] = False
                state["write_failed"] = True
                state["write_error"] = reason or "memoryguard write tool reported failure"
                changed = True
            else:
                state.pop("write_failed", None)
                state.pop("write_error", None)
                if not state.get("write_seen"):
                    # Unknown result; do not mark as success.
                    pass
        if changed:
            _save_state(root, normalized_provider, session_id, state)
        return {}

    if event == "pre_compact":
        state = _load_state(root, normalized_provider, session_id)
        if state.get("durable_candidate") and not state.get("write_seen"):
            if normalized_provider == "codex":
                return {}
            return _context_output(
                normalized_provider,
                event,
                _COMPACT_REMINDER,
            )
        return {}

    state = _load_state(root, normalized_provider, session_id)
    _flush_pending_rule_feedback(
        workspace=root,
        provider=normalized_provider,
        agent_instance_id=agent_instance_id,
        share_group_id=share_group_id,
        session_id=session_id,
        actor=f"hook:{normalized_provider}:{agent_instance_id}",
        trigger="stop_event",
    )
    last_message = str(payload.get("last_assistant_message", "") or "")
    candidate = bool(state.get("durable_candidate")) or _durable_candidate(
        last_message
    )
    already_continued = bool(
        state.get("stop_continued")
        or payload.get("stop_hook_active")
        or int(payload.get("loop_count", 0) or 0) > 0
    )
    if (
        mode == "enforce"
        and candidate
        and not state.get("write_seen")
        and not already_continued
    ):
        state["stop_continued"] = True
        _save_state(root, normalized_provider, session_id, state)
        return _stop_continue_output(
            normalized_provider,
            "本轮包含可能长期有效的偏好、纠正或默认规则，但尚无 "
            "MemoryGuard 写入回执。请只萃取稳定事实，调用一次 "
            "memoryguard_memory_write；不要保存整段对话，然后结束。",
        )
    return {}


def _read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read(2_000_000)
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("hook input must be a JSON object")
    return data


def _configure_utf8_stdio() -> None:
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name)
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        errors = "backslashreplace" if name == "stderr" else "strict"
        reconfigure(encoding="utf-8", errors=errors)


def _cmd_run(args: argparse.Namespace) -> int:
    _configure_utf8_stdio()
    try:
        payload = _read_stdin_json()
        result = run_hook(
            provider=args.provider,
            event=args.event,
            workspace=args.workspace,
            agent_instance_id=args.agent_id,
            share_group_id=args.share_group_id,
            payload=payload,
        )
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        # PreToolUse is the only fail-closed runtime event.  Other failures
        # surface through status/heartbeat without bricking the host session.
        if args.event == "pre_tool":
            result = _deny_output(
                args.provider,
                f"MemoryGuard Hook 执行失败，已阻止本次工具调用：{exc}",
            )
            sys.stdout.write(json.dumps(result, ensure_ascii=False))
            return 0
        sys.stderr.write(f"memoryguard hook error: {exc}\n")
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m memoryguard.host_hooks")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--provider", required=True)
    run.add_argument("--event", required=True, choices=tuple(_EVENT_NAMES))
    run.add_argument("--workspace", required=True)
    run.add_argument("--agent-id", required=True)
    run.add_argument("--share-group-id", required=True)
    run.add_argument("--managed-by", default="")
    run.set_defaults(func=_cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
