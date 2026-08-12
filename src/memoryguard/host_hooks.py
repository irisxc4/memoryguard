"""User-level host hooks for deterministic MemoryGuard takeover.

The Skill is the installer/trigger.  Enforcement lives in host lifecycle hooks
because a Skill only runs while it is active.  This module keeps host-specific
configuration behind one small interface and keeps runtime receipts free of
prompt or memory bodies.
"""

from __future__ import annotations

_V2_READ_STATES = frozenset({"V2_READY", "V2_ACTIVE"})

import argparse
import hashlib
import inspect
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

from .runtime_v2.public_safety import v2_upgrade_message, v2_upgrade_payload


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



def read_hook_stdin_json(stdin_buffer=None) -> dict[str, Any]:
    """Decode hook stdin JSON.

    Always read bytes and decode with utf-8-sig so Cursor BOM stdin and
    Codex plain UTF-8 both parse. Install/upgrade ships this path; hosts
    must not depend on emergency site-packages patches.
    """
    buf = sys.stdin.buffer if stdin_buffer is None else stdin_buffer
    raw = buf.read(2_000_000)
    if not raw or not raw.strip():
        return {}
    # Byte-level decode: strict UTF-8 first (current correct path, BOM-aware);
    # if the host shipped locale-encoded bytes (Windows GBK pipe) the intended
    # text is recovered instead of failing the whole hook.
    from .encoding_guard import decode_hook_bytes

    text = decode_hook_bytes(raw, source="hook_stdin")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("hook input must be a JSON object")
    return data


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
    session_id = str(
        payload.get("session_id")
        or payload.get("conversation_id")
        or payload.get("conversationId")
        or payload.get("sessionId")
        or ""
    ).strip()
    context_hash = str(payload.get("context_hash") or "").strip()
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
        session_id=session_id,
        context_hash=context_hash,
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


def _binding_plane_for_workspace(workspace: Path) -> str:
    """Return the authoritative binding plane for the current cutover state.

    V1_ACTIVE/V2_BUILDING are retired for host binding resolution.  They remain
    distinguishable for diagnostics, but callers must not use that result to
    construct or query a retired binding plane.
    """
    from .system.manifest import ManifestManager

    state = ManifestManager(workspace).current().state
    marker = str(getattr(state, "value", state) or "").strip().upper()
    if marker in {"V1_ACTIVE", "V2_BUILDING"}:
        raise ValueError(
            f"v2_upgrade_required: {v2_upgrade_message(marker, surface='Hook')}"
        )
    if marker in {"V2_READY", "V2_ACTIVE"}:
        return "v2"
    raise ValueError(
        "v2_manifest_state_unavailable: "
        f"{v2_upgrade_message('UNKNOWN', surface='Hook')}"
    )


def _validate_binding(
    workspace: Path,
    agent_instance_id: str,
    share_group_id: str,
) -> None:
    if not agent_instance_id:
        raise ValueError("agent_instance_id is required for hook installation")
    if not share_group_id:
        raise ValueError("share_group_id is required for hook installation")

    plane = _binding_plane_for_workspace(workspace)
    if plane != "v2":
        raise ValueError(v2_upgrade_message(plane, surface="Hook"))

    from .runtime_v2.group_native import GroupControlService

    binding = GroupControlService(workspace, write=False).active_binding_for_agent(
        agent_instance_id
    )
    matched = (
        binding is not None
        and str(binding.get("status") or "") == "active"
        and str(binding.get("share_group_id") or "") == str(share_group_id)
    )

    if not matched:
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
    mandatory_match_receipts: list[dict[str, Any]] | None = None,
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
            "mandatory_match_receipts": list(mandatory_match_receipts or []),
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


def derive_host_provider(payload: dict[str, Any]) -> str:
    """Provider identity derived from the payload shape alone ("" = unknown).

    ``run_hook`` stamps the provider from argv today without checking the
    payload, so a misconfigured hook can attribute one host's session to
    another (the observed dual-write: Cursor sessions archived as ``claude``).
    Payload shape gives an independent, conservative signal -- Cursor wraps
    lifecycle events in a nested envelope (``event.name`` / ``session`` dict),
    Claude Code emits a top-level ``hook_event_name``, and Codex carries no
    marker (argv stays authoritative there).  Empty string = unknown.
    """
    if not isinstance(payload, dict):
        return ""
    event = payload.get("event")
    if isinstance(event, dict) and isinstance(event.get("name"), str):
        return "cursor"
    if isinstance(payload.get("session"), dict):
        return "cursor"
    hook_event_name = payload.get("hook_event_name")
    if isinstance(hook_event_name, str) and hook_event_name.strip():
        return "claude"
    return ""


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


def _codex_active_thread_ids(payload: dict[str, Any]) -> set[str]:
    """Read an optional host-provided active-thread allowlist.

    ``CODEX_THREAD_ID`` remains the only trusted root identity.  The active
    list is merely a conservative no-touch boundary when Codex exposes it in
    a Stop payload or environment; malformed values are ignored.
    """
    from .codex_subagent_reconcile import _active_ids

    values: list[Any] = []
    for key in (
        "active_thread_ids",
        "active_threads",
        "active_subagent_thread_ids",
    ):
        raw = payload.get(key)
        if isinstance(raw, (list, tuple, set)):
            values.extend(raw)
        elif isinstance(raw, str):
            values.extend(part.strip() for part in raw.split(","))
    return _active_ids(values)


def _record_codex_reconcile_diagnostic(
    workspace: Path,
    provider: str,
    agent_instance_id: str,
    result: dict[str, Any],
) -> None:
    """Merge only a sanitized reconciliation summary into the heartbeat."""
    path = _heartbeat_path(workspace, provider, agent_instance_id)
    try:
        from .codex_subagent_reconcile import sanitize_reconcile_result

        receipt = _load_json_config(path, strict=False)
        receipt["codex_subagent_reconcile"] = sanitize_reconcile_result(result)
        receipt["at"] = _now_iso()
        _write_json_config(path, receipt)
    except Exception as exc:
        _emit_runtime_write_diagnostic(
            "codex_reconcile_receipt_write_failed", provider, "stop", exc
        )


def _best_effort_codex_reconcile(
    *,
    workspace: Path,
    agent_instance_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Reconcile Codex state without ever affecting the host hook result."""
    try:
        from .codex_subagent_reconcile import (
            codex_thread_matches_workspace,
            reconcile_codex_subagents,
            reconcile_global_codex_subagents,
            trusted_codex_thread_id,
        )

        # The environment is host-owned.  Never use payload ``thread_id`` as
        # a fallback because it can be prompt-controlled in synthetic hooks.
        root_thread_id = trusted_codex_thread_id()
        if not root_thread_id:
            return {}
        workspace_matches = codex_thread_matches_workspace(root_thread_id, workspace)
        active = _codex_active_thread_ids(payload) | {root_thread_id}
        result = reconcile_codex_subagents(
            root_thread_id,
            active_thread_ids=active,
            receipt_dir=workspace / ".memoryguard" / _RUNTIME_DIR / "codex-reconcile",
        )
        global_result = (
            reconcile_global_codex_subagents(
                active_thread_ids=active,
                receipt_dir=workspace / ".memoryguard" / _RUNTIME_DIR / "codex-reconcile",
            )
            if workspace_matches
            else {
                "status": "skipped",
                "reason": "thread_workspace_mismatch",
                "degraded": False,
                "closed_edge_count": 0,
                "archived_thread_count": 0,
            }
        )
        # Root Stop reconciliation remains the primary receipt.  Aggregate the
        # terminal-event sweep counts without copying global thread IDs into a
        # workspace heartbeat.
        result["global_reconcile"] = True
        result["global_status"] = str(global_result.get("status") or "")
        result["global_degraded"] = bool(global_result.get("degraded"))
        result["closed_edge_count"] = int(result.get("closed_edge_count") or 0) + int(
            global_result.get("closed_edge_count") or 0
        )
        result["archived_thread_count"] = int(
            result.get("archived_thread_count") or 0
        ) + int(global_result.get("archived_thread_count") or 0)
        result["open_edge_count"] = int(global_result.get("open_edge_count") or 0)
        result["skipped_nonterminal_count"] = int(
            global_result.get("skipped_nonterminal_count") or 0
        )
        result["terminal_event_counts"] = dict(
            global_result.get("terminal_event_counts") or {}
        )
        _record_codex_reconcile_diagnostic(
            workspace, "codex", agent_instance_id, result
        )
        if result.get("degraded") or global_result.get("degraded"):
            _emit_runtime_write_diagnostic(
                "codex_subagent_reconcile_degraded", "codex", "stop",
                RuntimeError(
                    str(
                        global_result.get("reason")
                        or result.get("reason")
                        or global_result.get("status")
                        or result.get("status")
                    )
                ),
            )
        return result
    except Exception as exc:
        # Stop is an observational seam; a state DB failure must not suppress
        # MemoryGuard's own feedback/continuation path or brick the session.
        _emit_runtime_write_diagnostic(
            "codex_subagent_reconcile_failed", "codex", "stop", exc
        )
        return {
            "version": "1",
            "provider": "codex",
            "ok": False,
            "degraded": True,
            "status": "degraded",
            "reason": f"reconcile_failed:{type(exc).__name__}",
        }


def _best_effort_codex_global_reconcile(
    *,
    workspace: Path,
    agent_instance_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Repair terminal stale tasks on startup without touching live branches."""

    try:
        from .codex_subagent_reconcile import (
            codex_thread_matches_workspace,
            reconcile_global_codex_subagents,
            trusted_codex_thread_id,
        )

        root_thread_id = trusted_codex_thread_id()
        if not root_thread_id or not codex_thread_matches_workspace(
            root_thread_id, workspace
        ):
            return {}
        result = reconcile_global_codex_subagents(
            active_thread_ids=_codex_active_thread_ids(payload) | {root_thread_id},
            receipt_dir=workspace / ".memoryguard" / _RUNTIME_DIR / "codex-reconcile",
        )
        _record_codex_reconcile_diagnostic(
            workspace, "codex", agent_instance_id, result
        )
        if result.get("degraded"):
            _emit_runtime_write_diagnostic(
                "codex_subagent_global_reconcile_degraded",
                "codex",
                "session_start",
                RuntimeError(str(result.get("reason") or result.get("status"))),
            )
        return result
    except Exception as exc:
        _emit_runtime_write_diagnostic(
            "codex_subagent_global_reconcile_failed",
            "codex",
            "session_start",
            exc,
        )
        return {
            "version": "2",
            "provider": "codex",
            "ok": False,
            "degraded": True,
            "status": "degraded",
            "reason": f"reconcile_failed:{type(exc).__name__}",
        }


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
    """Retired compatibility seam; history persistence is V2-native only."""
    return {
        "attempted": False,
        "archived": False,
        "reason": "v2_native_only",
        "event": event,
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
    compact = raw.replace("_", "").replace("-", "")
    if "callmcptool" not in compact and compact not in {"callmcptool"}:
        return ""
    if not isinstance(tool_input, dict):
        return ""
    inner = (
        tool_input.get("toolName")
        or tool_input.get("tool_name")
        or tool_input.get("name")
        or ""
    )
    if not inner and isinstance(tool_input.get("arguments"), dict):
        args = tool_input["arguments"]
        inner = args.get("toolName") or args.get("tool_name") or args.get("name") or ""
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
    raise RuntimeError(v2_upgrade_message("UNKNOWN", surface="Hook"))


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
    store: Any,
    receipts: list[dict[str, Any]],
    provider: str,
    event: str,
) -> None:
    """Retired compatibility seam; native V2 owns receipt persistence."""
    return None


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
    """Retired compatibility seam; native V2 owns feedback persistence."""
    return None



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


def _v2_upgrade_output(provider: str, event: str, state: str) -> dict[str, Any]:
    """Fail closed with stable retirement guidance for every non-V2 state."""
    payload = v2_upgrade_payload(state, surface="Hook")
    reason = v2_upgrade_message(state, surface="Hook")
    if event == "pre_tool":
        result = _deny_output(provider, reason)
    elif event == "stop":
        result = _stop_continue_output(provider, reason)
    elif provider == "cursor" and event == "user_prompt":
        result = {"continue": False}
    else:
        result = _context_output(provider, event, reason)
    result.update(payload)
    nested = result.get("hookSpecificOutput")
    if isinstance(nested, dict):
        nested.update({
            "code": payload["code"],
            "error": payload["error"],
            "state": payload["state"],
            "next_step": payload["next_step"],
        })
    return result


# ---------------------------------------------------------------------------
# Phase6 V2 host-hook seam
# ---------------------------------------------------------------------------

_V2_STATES = frozenset({"V1_ACTIVE", "V2_BUILDING", "V2_READY", "V2_ACTIVE"})
_V2_READ_STATES = frozenset({"V2_READY", "V2_ACTIVE"})
_V2_FACADE_MISSING = object()
_v2_runtime_facade_factory: Any = None


def _load_v2_runtime_facade(workspace: Path) -> Any:
    """Load the native V2 facade; never fall back to retired storage."""
    factory = globals().get("_v2_runtime_facade_factory")
    if callable(factory):
        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("v2_runtime_factory_signature_unavailable") from exc
        try:
            signature.bind(workspace)
        except TypeError:
            try:
                signature.bind(workspace=workspace)
            except TypeError as exc:
                raise RuntimeError("v2_runtime_factory_signature_unavailable") from exc
            return factory(workspace=workspace)
        return factory(workspace)

    from .cutover_v2.facade import get_v2_runtime_facade
    return get_v2_runtime_facade(str(workspace))


def _v2_state(value: Any) -> str:
    # Host hooks consume an injected facade snapshot.  Require the same
    # trusted RuntimeSnapshot factory semantics as MCP; malformed generation,
    # unavailable/error envelopes, and hand-built snapshots fail closed.
    try:
        from .cutover_v2.state import CutoverState, RuntimeSnapshot
        if isinstance(value, RuntimeSnapshot):
            if not value.trusted or not value.available:
                return "UNKNOWN"
            return value.state.value if value.generation >= 0 else "UNKNOWN"
        if isinstance(value, CutoverState):
            return "UNKNOWN"
        if isinstance(value, dict) and any(key in value for key in ("state", "manifest_state", "status", "marker")):
            snapshot = RuntimeSnapshot.from_value(value)
            return snapshot.state.value if snapshot.available else "UNKNOWN"
        if hasattr(value, "state"):
            snapshot = RuntimeSnapshot.from_value(value)
            return snapshot.state.value if snapshot.available else "UNKNOWN"
    except Exception:
        return "UNKNOWN"
    enum_value = getattr(value, "value", None)
    if enum_value is not None and enum_value is not value:
        value = enum_value
    object_state = getattr(value, "state", None)
    if object_state is not None and object_state is not value:
        return _v2_state(object_state)
    if isinstance(value, dict):
        for key in ("state", "manifest_state", "status", "marker"):
            if key in value:
                return _v2_state(value[key])
        for key in ("manifest", "snapshot"):
            if isinstance(value.get(key), dict):
                return _v2_state(value[key])
        return "UNKNOWN"
    marker = str(value or "").strip().upper()
    return marker if marker in _V2_STATES else "UNKNOWN"


def _v2_facade_snapshot(facade: Any) -> tuple[str, Any]:
    fn = getattr(facade, "state_snapshot", None)
    if not callable(fn):
        fn = getattr(facade, "status", None)
    if not callable(fn):
        return "UNKNOWN", None
    try:
        value = fn()
        return _v2_state(value), value
    except Exception:
        return "UNKNOWN", None



def _v2_facade_snapshot(facade: Any) -> tuple[str, Any]:
    fn = getattr(facade, "state_snapshot", None)
    if not callable(fn):
        fn = getattr(facade, "status", None)
    if not callable(fn):
        return "UNKNOWN", None
    try:
        value = fn()
        return _v2_state(value), value
    except Exception:
        return "UNKNOWN", None


def _plain_hook_context(context: Any) -> dict[str, Any]:
    if isinstance(context, dict):
        return dict(context)
    try:
        value = asdict(context)
    except (TypeError, ValueError):
        try:
            value = dict(vars(context))
        except (TypeError, AttributeError):
            value = {}
    return dict(value) if isinstance(value, dict) else {}


def _v2_hook_context(
    provider: str,
    agent_instance_id: str,
    share_group_id: str,
    payload: dict[str, Any],
    event: str,
    workspace: Path,
) -> Any:
    context = _effective_agent_context(
        provider, agent_instance_id, share_group_id, payload, event=event,
    )
    plain = _plain_hook_context(context)
    try:
        from .access_context import AccessContext, load_access_context
        from .runtime_v2.group_native import GroupControlService
        from .runtime_v2.native_ports import bind_native_transport_context

        access = load_access_context()
        resolved_agent, error = access.resolve_agent(agent_instance_id)
        if error or resolved_agent != agent_instance_id:
            raise ValueError("trusted hook agent unavailable")
        binding = GroupControlService(workspace, write=False).active_binding_for_agent(
            agent_instance_id,
        )
        if (
            not binding
            or str(binding.get("status") or "") != "active"
            or str(binding.get("share_group_id") or "") != str(share_group_id)
        ):
            raise ValueError("active V2 hook binding unavailable")
        if type(access) is not AccessContext:
            raise ValueError("trusted hook context capability unavailable")
        return bind_native_transport_context(
            access,
            workspace_id=str(workspace),
            share_group_id=str(share_group_id),
            project_ref=str(getattr(context, "project_ref", "") or ""),
            provider=str(getattr(context, "provider", provider) or provider),
            runtime_role=str(getattr(context, "runtime_role", "") or ""),
            runtime_agent_id=str(getattr(context, "runtime_agent_id", "") or ""),
            parent_agent_id=str(getattr(context, "parent_agent_id", "") or ""),
            context_hash=str(getattr(context, "context_hash", "") or ""),
            entrypoint="hook",
        )
    except Exception:
        # Injected test facades may intentionally accept the serializable
        # context form.  A real native port rejects that form before mutation.
        return plain


def _hook_data(result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    data = dict(result)
    embedded = data.get("data")
    if isinstance(embedded, dict):
        # The outer transport envelope is always ``status=ok`` when dispatch
        # itself succeeded.  The ContextPacket inside ``data`` owns the
        # semantic status/error (including mandatory overflow), so it must win
        # for those fields rather than being hidden by the transport status.
        data = {**data, **embedded}
    packet = data.get("packet") or data.get("context_packet")
    if not isinstance(packet, dict) and any(
        key in data for key in ("mandatory", "relevant", "receipts", "ready", "effective_agent")
    ):
        packet = {
            key: data[key]
            for key in (
                "mandatory", "relevant", "knowledge", "reference_only",
                "budget", "effective_agent", "receipts", "ready", "state",
                "status", "error",
            )
            if key in data
        }
    return data, packet if isinstance(packet, dict) else {}


def _normalize_native_v2_packet(
    packet: dict[str, Any],
    *,
    workspace: Path,
    provider: str,
    agent_instance_id: str,
    share_group_id: str,
    session_id: str,
    event: str,
) -> dict[str, Any]:
    """Project a native ContextPacket onto the Hook receipt boundary.

    V2 deliberately does not expose the old rule-store receipt object.  Its
    bounded ContextEngine receipts are the authoritative match evidence.  The
    Hook keeps only opaque, deterministic IDs and public metadata, never rule
    bodies or raw evidence, so Cursor session injection and the next-tool gate
    share one fail-closed receipt shape.
    """
    if not any(key in packet for key in ("mandatory", "relevant", "receipts")):
        return packet
    normalized = dict(packet)
    mandatory = [item for item in packet.get("mandatory", []) if isinstance(item, dict)]
    relevant = [item for item in packet.get("relevant", []) if isinstance(item, dict)]
    normalized["mandatory_items"] = mandatory
    normalized["items"] = relevant
    rule_ids = [
        str(item.get("item_id") or item.get("memory_id") or "").strip()
        for item in mandatory
    ]
    rule_ids = [value for value in rule_ids if value]
    receipts: list[dict[str, Any]] = []
    for item in packet.get("receipts", []):
        if not isinstance(item, dict):
            continue
        if item.get("hit") is not True or str(item.get("layer") or "") != "mandatory":
            continue
        item_id = str(item.get("item_id") or item.get("memory_id") or "").strip()
        if not item_id:
            continue
        receipt_id = "v2-" + _short_hash(json.dumps(
            {
                "workspace": str(workspace),
                "provider": provider,
                "agent": agent_instance_id,
                "group": share_group_id,
                "session": session_id,
                "event": event,
                "item": item_id,
                "digest": item.get("digest") or item.get("item_hash") or "",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ))
        receipts.append({
            "receipt_id": receipt_id,
            "memory_id": item_id,
            "item_id": item_id,
            "layer": "mandatory",
            "source": "native-v2-context",
        })
    normalized["mandatory_rule_ids"] = rule_ids
    normalized["mandatory_match_receipts"] = receipts
    status = str(packet.get("status") or "").casefold()
    error = str(packet.get("error") or "").strip()
    normalized["mandatory_overflow"] = bool(
        packet.get("mandatory_overflow")
        or status in {"blocked", "error", "failed"}
        or error.startswith("mandatory_")
    )
    normalized["mandatory_invalid_reason"] = str(
        packet.get("mandatory_invalid_reason") or error or ""
    )
    return normalized


def _v2_hook_cutover(
    *,
    provider: str,
    event: str,
    workspace: Path,
    agent_instance_id: str,
    share_group_id: str,
    session_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Route every Hook event through the V2 state gate and native hook port."""
    try:
        facade = _load_v2_runtime_facade(workspace)
    except Exception:
        return _v2_upgrade_output(provider, event, "UNKNOWN")
    if facade is _V2_FACADE_MISSING:
        return _v2_upgrade_output(provider, event, "UNKNOWN")
    state, snapshot = _v2_facade_snapshot(facade)
    if state not in _V2_READ_STATES:
        return _v2_upgrade_output(provider, event, state)
    if state == "V2_READY" and event == "stop":
        return {}

    hook = getattr(facade, "bootstrap_hook", None)
    if not callable(hook):
        return _context_output(
            provider, event,
            "MemoryGuard V2 hook capability unavailable; bootstrap blocked.",
        )
    try:
        params = inspect.signature(hook).parameters
        has_context = "context" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        accepts_snapshot = "snapshot" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except (TypeError, ValueError):
        has_context = False
        accepts_snapshot = False
    if not has_context:
        return _context_output(
            provider, event,
            "MemoryGuard V2 hook context capability unavailable; bootstrap blocked.",
        )
    try:
        context = _v2_hook_context(
            provider, agent_instance_id, share_group_id, payload, event, workspace,
        )
        kwargs: dict[str, Any] = {"context": context}
        if accepts_snapshot:
            kwargs["snapshot"] = snapshot
        result = hook(event, dict(payload), **kwargs)
    except Exception as exc:
        _record_heartbeat(
            workspace, provider, agent_instance_id, event=event,
            error=f"v2_hook_dispatch_failed:{type(exc).__name__}",
        )
        if event == "pre_tool":
            return _deny_output(provider, "MemoryGuard V2 hook failed; tool execution denied.")
        return _context_output(provider, event, "MemoryGuard V2 hook failed; bootstrap blocked.")

    if not isinstance(result, dict):
        reason = "v2 hook returned invalid envelope"
        state_payload = _load_state(workspace, provider, session_id)
        state_payload.update({
            "bootstrap_ok": False,
            "bootstrap_error": reason,
            "mandatory_overflow": True,
            "mandatory_invalid_reason": reason,
            "mandatory_match_receipts": [],
        })
        _save_state(workspace, provider, session_id, state_payload)
        _record_heartbeat(
            workspace, provider, agent_instance_id, event=event, error=reason,
            mandatory_overflow=True,
        )
        if event == "pre_tool":
            return _deny_output(provider, "MemoryGuard V2 hook returned an invalid envelope; bootstrap blocked.")
        return _context_output(provider, event, "MemoryGuard V2 hook returned an invalid envelope; bootstrap blocked.")

    data, packet = _hook_data(result)
    packet = _normalize_native_v2_packet(
        packet,
        workspace=workspace,
        provider=provider,
        agent_instance_id=agent_instance_id,
        share_group_id=share_group_id,
        session_id=session_id,
        event=event,
    )
    status = str(data.get("status", "") or "").strip().casefold()
    if data.get("ok") is False or status in {"error", "blocked", "failed"} or data.get("error"):
        reason = str(data.get("error") or data.get("code") or "v2_hook_bootstrap_failed")
        state_payload = _load_state(workspace, provider, session_id)
        state_payload.update({
            "bootstrap_ok": False,
            "bootstrap_error": reason[:500],
            "mandatory_overflow": True,
            "mandatory_invalid_reason": reason[:500],
            "mandatory_match_receipts": [],
        })
        _save_state(workspace, provider, session_id, state_payload)
        _record_heartbeat(
            workspace, provider, agent_instance_id, event=event,
            error=f"v2_hook_envelope_failed:{reason}",
            mandatory_overflow=True,
        )
        if event == "pre_tool":
            return _deny_output(provider, "MemoryGuard V2 hook failed; tool execution denied.")
        if event == "stop":
            return _stop_continue_output(provider, "MemoryGuard V2 hook failed; stop blocked.")
        if packet.get("mandatory_overflow"):
            return _context_output(
                provider,
                event,
                _render_context({"context_packet": packet, **packet}),
            )
        return _context_output(provider, event, "MemoryGuard V2 hook failed; bootstrap blocked.")

    if event in {"session_start", "subagent_start", "user_prompt", "stop"}:
        receipts = packet.get(
            "mandatory_match_receipts",
            data.get("mandatory_match_receipts", []),
        )
        state_payload = _load_state(workspace, provider, session_id)
        bootstrap_ok = not bool(
            packet.get("mandatory_overflow", data.get("mandatory_overflow", False))
        )
        # Cursor's beforeSubmitPrompt hook is only the conversation receipt
        # boundary.  Its first ordinary tool must remain locked until the
        # host has actually executed memoryguard_context_bootstrap through
        # CallMcpTool; otherwise a successful prompt hook would silently
        # bypass the provider's explicit bootstrap gate.
        if provider == "cursor" and event == "user_prompt":
            bootstrap_ok = False
        state_payload.update({
            "bootstrap_ok": bootstrap_ok,
            "mandatory_overflow": bool(packet.get("mandatory_overflow", data.get("mandatory_overflow", False))),
            "mandatory_invalid_reason": str(
                packet.get("mandatory_invalid_reason", data.get("error", "")) or ""
            ),
            "mandatory_rule_ids": list(
                packet.get("mandatory_rule_ids", data.get("mandatory_rule_ids", [])) or []
            ),
            "mandatory_match_receipts": receipts if isinstance(receipts, list) else [],
        })
        if event == "user_prompt":
            state_payload.update({
                "prompt_hash": _short_hash(_prompt(payload)),
                "durable_candidate": _durable_candidate(_prompt(payload)),
                "write_seen": False,
                "stop_continued": False,
            })
        _save_state(workspace, provider, session_id, state_payload)

    heartbeat_overflow = bool(
        packet.get("mandatory_overflow", data.get("mandatory_overflow", False))
    )
    if event == "pre_tool":
        # A pre-tool request may resolve a different runtime role than the
        # lifecycle event that discovered an overflow (notably a subagent
        # start followed by the parent host's tool gate).  Preserve the
        # session's fail-closed overflow receipt instead of overwriting it
        # with the empty packet from that unrelated scope.
        heartbeat_overflow = heartbeat_overflow or bool(
            _load_state(workspace, provider, session_id).get("mandatory_overflow")
        )
    _record_heartbeat(
        workspace, provider, agent_instance_id, event=event,
        error=str(packet.get("error", data.get("error", "")) or ""),
        mandatory_rule_ids=list(
            packet.get("mandatory_rule_ids", data.get("mandatory_rule_ids", [])) or []
        ),
        mandatory_match_receipts=(
            packet.get("mandatory_match_receipts", data.get("mandatory_match_receipts", []))
            if isinstance(packet.get("mandatory_match_receipts", data.get("mandatory_match_receipts", [])), list)
            else []
        ),
        mandatory_overflow=heartbeat_overflow,
    )

    direct_output = data.get("output") or data.get("host_output")
    if isinstance(direct_output, dict):
        return direct_output
    text = str(data.get("text", "") or "")
    if not text and packet:
        text = _render_context({"context_packet": packet, **packet})
    if event == "session_start":
        text = _static_session_context(provider) + ("\n" + text if text else "")
        return _context_output(provider, event, text)
    if event == "subagent_start":
        return _context_output(
            provider,
            event,
            _static_session_context(provider) + ("\n" + text if text else ""),
        )
    if event == "user_prompt":
        return _context_output(provider, event, text) if text else _allow_output(provider, event)
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
    """Run one host Hook event through the V2 state gate."""
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

    # Codex thread cleanup is a host-owned lifecycle side effect.  Run it
    # before the V2 memory data-plane gate so stale terminal edges are repaired
    # even when the workspace still needs upgrade.  Stop remains observational
    # and best-effort; an upgrade response must not block host shutdown.
    if normalized_provider == "codex":
        if event == "session_start":
            _best_effort_codex_global_reconcile(
                workspace=root,
                agent_instance_id=agent_instance_id,
                payload=payload,
            )
        elif event == "stop":
            _best_effort_codex_reconcile(
                workspace=root,
                agent_instance_id=agent_instance_id,
                payload=payload,
            )

    v2_result = _v2_hook_cutover(
        provider=normalized_provider,
        event=event,
        workspace=root,
        agent_instance_id=agent_instance_id,
        share_group_id=share_group_id,
        session_id=session_id,
        payload=payload,
    )

    # Retired/unknown states already carry the stable public error.  Return
    # before any local guard or compatibility helper can run.
    result_code = str(v2_result.get("code", "") or "")
    if result_code in {
        "v2_upgrade_required",
        "v2_manifest_state_unavailable",
    }:
        if normalized_provider == "codex" and event == "stop":
            return {}
        return v2_result

    if event == "pre_tool":
        state = _load_state(root, normalized_provider, session_id)
        if state.get("mandatory_overflow") and mode == "enforce":
            return _deny_output(
                normalized_provider,
                "MemoryGuard 强制规则包异常，停止继续执行。请先修复共享记忆中的强制规则。",
            )
        if state.get("bootstrap_error") and mode == "enforce":
            return _deny_output(
                normalized_provider,
                "MemoryGuard 上下文加载不可用，工具执行已安全停止。请检查绑定或 Hook 状态。",
            )
        tool_name = str(payload.get("tool_name", "") or "")
        tool_input = payload.get("tool_input", {})
        if _targets_native_memory(tool_name, tool_input):
            reason = (
                "MemoryGuard 已接管长期记忆：禁止 Agent 写入宿主原生记忆路径。"
                "请改用 memoryguard_memory_write；人工 GUI 删除/恢复不受影响。"
            )
            if mode == "enforce":
                return _deny_output(normalized_provider, reason)
        elif _is_other_memory_write(tool_name):
            reason = (
                "检测到其他记忆 MCP 写入。正式接管模式只允许 "
                "MemoryGuard 作为长期记忆写入端。"
            )
            if mode == "enforce":
                return _deny_output(normalized_provider, reason)
        if normalized_provider == "cursor":
            if _is_memoryguard_bootstrap(tool_name, tool_input):
                if not state.get("mandatory_overflow"):
                    state["bootstrap_ok"] = True
                    _save_state(root, normalized_provider, session_id, state)
                return v2_result
            is_subagent = bool(
                payload.get("subagent_id") or payload.get("agent_id")
            )
            if (
                mode == "enforce"
                and state
                and not state.get("bootstrap_ok")
                and not is_subagent
            ):
                return _deny_output(
                    normalized_provider,
                    "开始本轮工具操作前，先调用 "
                    "memoryguard_context_bootstrap(task=当前用户请求)。",
                )
        return v2_result

    if event == "post_tool":
        state = _load_state(root, normalized_provider, session_id)
        tool_name = str(payload.get("tool_name", "") or "")
        tool_input = payload.get("tool_input", {})
        tool_result = payload.get("tool_result", payload.get("result"))
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
                state["write_failed"] = True
                state["write_error"] = reason or "memoryguard write tool reported failure"
                changed = True
        if changed:
            _save_state(root, normalized_provider, session_id, state)
        return v2_result

    if event == "pre_compact" and not v2_result:
        state = _load_state(root, normalized_provider, session_id)
        if state.get("durable_candidate") and not state.get("write_seen"):
            if normalized_provider == "codex":
                return {}
            return _context_output(
                normalized_provider, event, _COMPACT_REMINDER,
            )

    return v2_result



def _read_stdin_json() -> dict[str, Any]:
    """CLI entrypoint: binary utf-8-sig decode (Cursor BOM-safe)."""
    return read_hook_stdin_json()


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
