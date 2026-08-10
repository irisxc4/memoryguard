"""Native, read-only External MCP inventory and preview service.

The legacy :mod:`memoryguard.external_mcp_detector` is intentionally not used
at this boundary.  Its detection path persists a descriptor and its import
path opens the legacy governance store.  This service reads one already
existing, workspace-owned ``servers.json`` and exposes only an opaque server
reference plus bounded capability/content metadata.

The service is deliberately transport-facing rather than a replacement for
the detector.  It accepts a process-issued native transport context, never a
plain identity mapping, and never accepts a caller-selected config path.
Missing configuration is a successful ``NO_SOURCE`` response; no directory,
file, database, or legacy store is created as a side effect.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable, Mapping

from ..governance_lock import WorkspaceGovernanceLock
from .native_ports import (
    NativeContextError,
    NativePortError,
    resolve_native_transport_context,
)


SERVICE_NAME = "external_mcp_native"
CONFIG_RELATIVE_PATH = Path(".memoryguard") / "external-mcp" / "servers.json"
MAX_CONFIG_BYTES = 8 * 1024 * 1024
MAX_SERVERS = 1_000
MAX_ITEMS = 10_000
MAX_TEXT_BYTES = 4 * 1024 * 1024
MAX_INPUT_SCHEMA_DEPTH = 16
MAX_INPUT_SCHEMA_NODES = 2_048
MAX_INPUT_SCHEMA_BYTES = 128 * 1024

_REPARSE_ATTRIBUTE = 0x0400  # FILE_ATTRIBUTE_REPARSE_POINT on Windows

_PAYLOAD_PATH_KEYS = frozenset(
    {
        "path", "config_path", "config", "file", "file_path", "source_path",
        "workspace", "workspace_path", "root", "root_path", "directory",
    },
)
_PAYLOAD_IDENTITY_KEYS = frozenset(
    {
        "workspace_id", "workspace", "agent", "agent_id", "agent_instance_id",
        "trusted_agent_id", "share_group_id", "group", "group_id", "project",
        "project_id", "project_ref", "provider", "runtime", "runtime_role",
    },
)
_SECRET_KEYS = frozenset(
    {
        "secret", "secrets", "token", "tokens", "access_token", "refresh_token",
        "api_key", "apikey", "password", "passwd", "credential", "credentials",
        "private_key", "authorization", "cookie", "cookies", "command", "cmd",
        "argv", "args", "env", "environment", "headers", "header",
    },
)
# Keep value markers derived from the same canonical set as secret keys.  The
# matcher below applies token boundaries, so ``tokenizer`` is not treated as
# a secret merely because it contains ``token``.
_SECRET_VALUE_MARKERS = _SECRET_KEYS

_ROOT_KEYS = frozenset({"servers", "schema", "schema_version"})
_SERVER_KEYS = frozenset(
    {
        "server_id", "server_ref", "display_name", "name", "provider", "type", "kind",
        "level", "tool_count", "resource_count", "memory_entry_count",
        "safe_to_auto_call_tools", "import_strategy", "detected_at", "descriptor",
        "tools", "resources", "memory_entries", "capabilities", "share_group_id",
        "project_ref", "scope", "agent_instance_id",
    },
)
_DESCRIPTOR_KEYS = frozenset(
    {
        "name", "display_name", "provider", "type", "kind", "level", "tools",
        "resources", "memory_entries", "capabilities",
    },
)
_TOOL_KEYS = frozenset({"name", "title", "description", "inputSchema"})
_RESOURCE_KEYS = frozenset({"name", "uri", "mimeType", "description", "text", "content"})
_MEMORY_KEYS = frozenset({"body", "metadata", "kind", "source"})
_CAPABILITY_KEYS = frozenset({"tools", "resources", "memory_entries", "known_memory"})
_KNOWN_LEVELS = frozenset(
    {
        "L0_unrecognizable", "L1_unknown_tools", "L2_generic_resources",
        "L3_known_memory_mcp", "L4_memoryguard_mcp",
    },
)


class ExternalMCPNativeError(NativePortError):
    """Stable, non-leaking read-boundary error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)


_TEST_CAPABILITY = object()


@dataclass(frozen=True, slots=True)
class ExternalMCPTestCapability:
    """Process-local test seam; plain mappings/callables are not accepted."""

    token: object
    servers: tuple[Mapping[str, Any], ...] = ()
    reader: Callable[..., Any] | None = None

    def __post_init__(self) -> None:
        if self.token is not _TEST_CAPABILITY:
            raise ExternalMCPNativeError("external_mcp_test_capability_required")


def bind_external_mcp_test_capability(
    *,
    servers: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] | None = None,
    reader: Callable[..., Any] | None = None,
) -> ExternalMCPTestCapability:
    """Issue an in-process fixture capability for tests only.

    ``reader`` is invoked at most once per service read.  A ``TypeError``
    raised by reader code is never retried with another argument shape.
    """

    if servers is not None and reader is not None:
        raise ExternalMCPNativeError("external_mcp_test_capability_conflict")
    values = tuple(servers or ())
    if any(not isinstance(item, Mapping) for item in values):
        raise ExternalMCPNativeError("external_mcp_test_servers_required")
    if reader is not None and not callable(reader):
        raise ExternalMCPNativeError("external_mcp_test_reader_required")
    return ExternalMCPTestCapability(_TEST_CAPABILITY, values, reader)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _canonical(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ExternalMCPNativeError("external_mcp_path_resolution_failed") from exc


def _reparse_point(path: Path) -> bool:
    """Detect symlinks and Windows reparse points without following them."""

    try:
        current = path
        while True:
            try:
                info = current.lstat()
            except FileNotFoundError:
                current = current.parent
                if current == current.parent:
                    break
                continue
            if stat.S_ISLNK(info.st_mode):
                return True
            if int(getattr(info, "st_file_attributes", 0) or 0) & _REPARSE_ATTRIBUTE:
                return True
            if current == current.parent:
                break
            current = current.parent
    except OSError:
        return True
    return False


def _is_reparse_info(info: Any) -> bool:
    return bool(
        stat.S_ISLNK(getattr(info, "st_mode", 0))
        or int(getattr(info, "st_file_attributes", 0) or 0) & _REPARSE_ATTRIBUTE
    )


def _stat_identity(info: Any) -> tuple[Any, ...]:
    """Capture stable identity/metadata without exposing platform details."""

    mode, device, inode = _validated_object_identity(info)
    return (
        device,
        inode,
        stat.S_IFMT(mode),
        getattr(info, "st_size", None),
        getattr(info, "st_mtime_ns", getattr(info, "st_mtime", None)),
        getattr(info, "st_ctime_ns", getattr(info, "st_ctime", None)),
    )


def _object_identity(info: Any) -> tuple[Any, ...]:
    """Return only the object identity fields used for fd/path binding."""

    mode, device, inode = _validated_object_identity(info)
    return device, inode, stat.S_IFMT(mode)


def _validated_object_identity(info: Any) -> tuple[int, int, int]:
    """Return identity fields or fail closed when the platform cannot bind it.

    ``st_size``/timestamps are attacker-replayable metadata and are never a
    substitute for an object identity.  In particular, some platforms and
    test doubles report ``st_ino == 0``; accepting that value would make a
    replaced path indistinguishable from the original.  The same check is
    used for every parent component and for the opened descriptor.
    """

    try:
        mode = int(getattr(info, "st_mode"))
        device = int(getattr(info, "st_dev"))
        inode = int(getattr(info, "st_ino"))
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ExternalMCPNativeError("external_mcp_path_changed") from exc
    if mode <= 0 or device < 0 or inode <= 0:
        raise ExternalMCPNativeError("external_mcp_path_changed")
    return mode, device, inode


def _same_object(left: Any, right: Any) -> bool:
    return _object_identity(left) == _object_identity(right)


def _chain_paths(root: Path, target: Path) -> tuple[Path, ...]:
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ExternalMCPNativeError("path_out_of_scope") from exc
    paths = [root]
    current = root
    for component in relative.parts:
        current = current / component
        paths.append(current)
    return tuple(paths)


def _snapshot_path_chain(root: Path, target: Path) -> tuple[dict[Path, tuple[Any, ...]], bool]:
    """Snapshot every allowed path component with lstat (never follows links).

    The boolean is true only when the target exists. Missing parents/target
    are represented by a partial snapshot so callers can distinguish a clean
    ``NO_SOURCE`` from a path that changed while opening/reading.
    """

    snapshot: dict[Path, tuple[Any, ...]] = {}
    for path in _chain_paths(root, target):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return snapshot, False
        except OSError as exc:
            raise ExternalMCPNativeError("external_mcp_source_unavailable") from exc
        if _is_reparse_info(info):
            raise ExternalMCPNativeError("reparse_point_blocked")
        snapshot[path] = _stat_identity(info)
    return snapshot, True


def _same_snapshot(left: Mapping[Path, tuple[Any, ...]], right: Mapping[Path, tuple[Any, ...]]) -> bool:
    return dict(left) == dict(right)


def _contains_secret_marker(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", _text(value).casefold()).strip("_")
    if not normalized:
        return False
    # Markers are normalized exactly as keys and matched only at token
    # boundaries.  This catches ``api-key``/``auth_token`` and values such as
    # ``password=...`` while allowing words like ``tokenizer``.
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(re.sub(r'[^a-z0-9]+', '_', marker.casefold()).strip('_'))}(?![a-z0-9])", normalized)
        for marker in _SECRET_VALUE_MARKERS
    )


def _safe_label(value: Any, default: str = "unknown") -> str:
    value = _text(value)
    if not value or len(value) > 256:
        return default
    if Path(value).is_absolute() or value.startswith(("\\\\", "/")):
        return default
    if _contains_secret_marker(value) or "\n" in value or "\r" in value:
        return default
    # Command/path-like labels are not useful in the UI and are easy to turn
    # into an accidental disclosure, even when a key was omitted.
    if ":\\" in value or ":/" in value:
        return default
    return value[:128]


def _digest(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()


def _server_ref(server_id: str) -> str:
    return f"external-mcp:{_digest(server_id)[:24]}"


def _entry_ref(server_ref: str, kind: str, index: int, content_digest: str) -> str:
    return f"external-entry:{_digest((server_ref, kind, index, content_digest))[:24]}"


def _is_secret_key(key: Any) -> bool:
    return _contains_secret_marker(_text(key))


def _ensure_mapping(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExternalMCPNativeError(code)
    return value


def _check_keys(value: Mapping[str, Any], allowed: frozenset[str]) -> None:
    for key in value:
        text = str(key)
        if _is_secret_key(text):
            raise ExternalMCPNativeError("external_mcp_secret_field")
        if text not in allowed:
            raise ExternalMCPNativeError("external_mcp_unknown_field")


def _validate_scalar_text(value: Any, code: str = "external_mcp_invalid_field") -> str:
    if not isinstance(value, str) or len(value.encode("utf-8", "replace")) > MAX_TEXT_BYTES:
        raise ExternalMCPNativeError(code)
    return value


def _validate_input_schema(value: Any) -> None:
    """Validate a bounded JSON-Schema tree without retaining/forwarding it."""

    if not isinstance(value, Mapping):
        raise ExternalMCPNativeError("invalid_external_mcp_input_schema")

    # Budget-check before json.dumps.  A recursive serializer can otherwise
    # hit Python's recursion limit on an attacker-controlled 10k-deep schema
    # before our own depth/node limits run.  Iteration also keeps this check
    # bounded for hostile recursive containers.
    nodes = 0
    pending: list[tuple[Any, int]] = [(value, 0)]
    seen_containers: set[int] = set()
    try:
        while pending:
            node, depth = pending.pop()
            if depth > MAX_INPUT_SCHEMA_DEPTH:
                raise ExternalMCPNativeError("external_mcp_input_schema_too_deep")
            nodes += 1
            if nodes > MAX_INPUT_SCHEMA_NODES:
                raise ExternalMCPNativeError("external_mcp_input_schema_node_limit")
            if isinstance(node, Mapping):
                identity = id(node)
                if identity in seen_containers:
                    raise ExternalMCPNativeError("invalid_external_mcp_input_schema")
                seen_containers.add(identity)
                for key, child in node.items():
                    if not isinstance(key, str):
                        raise ExternalMCPNativeError("external_mcp_input_schema_key_type")
                    if _is_secret_key(key):
                        raise ExternalMCPNativeError("external_mcp_secret_field")
                    if nodes + len(pending) + 1 > MAX_INPUT_SCHEMA_NODES:
                        raise ExternalMCPNativeError("external_mcp_input_schema_node_limit")
                    pending.append((child, depth + 1))
                continue
            if isinstance(node, list):
                identity = id(node)
                if identity in seen_containers:
                    raise ExternalMCPNativeError("invalid_external_mcp_input_schema")
                seen_containers.add(identity)
                for child in node:
                    if nodes + len(pending) + 1 > MAX_INPUT_SCHEMA_NODES:
                        raise ExternalMCPNativeError("external_mcp_input_schema_node_limit")
                    pending.append((child, depth + 1))
                continue
            if isinstance(node, str):
                if _contains_secret_marker(node):
                    raise ExternalMCPNativeError("external_mcp_secret_field")
                continue
            if node is None or isinstance(node, (bool, int, float)):
                continue
            raise ExternalMCPNativeError("invalid_external_mcp_input_schema")

        try:
            serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (RecursionError, MemoryError, TypeError, ValueError, OverflowError) as exc:
            raise ExternalMCPNativeError("invalid_external_mcp_input_schema") from exc
        try:
            serialized_bytes = serialized.encode("utf-8")
        except (AttributeError, MemoryError, TypeError, UnicodeError) as exc:
            raise ExternalMCPNativeError("invalid_external_mcp_input_schema") from exc
    except ExternalMCPNativeError:
        raise
    except (RecursionError, MemoryError, TypeError) as exc:
        raise ExternalMCPNativeError("invalid_external_mcp_input_schema") from exc
    if len(serialized_bytes) > MAX_INPUT_SCHEMA_BYTES:
        raise ExternalMCPNativeError("external_mcp_input_schema_too_large")


@dataclass(frozen=True, slots=True)
class _ServerRecord:
    server_id: str
    server_ref: str
    provider: str
    type: str
    level: str
    tools: tuple[Mapping[str, Any] | str, ...]
    resources: tuple[Mapping[str, Any] | str, ...]
    memory_entries: tuple[Mapping[str, Any], ...]
    share_group_id: str
    project_ref: str
    display_name: str


class NativeExternalMCPService:
    """Native External MCP descriptor inventory, preview and governed import.

    Import persists only a validated static descriptor.  It never connects to
    or invokes an external MCP server, so unknown tools cannot execute merely
    because a descriptor was imported.
    """

    service_name = SERVICE_NAME

    def __init__(self, workspace: str | Path, *, test_capability: ExternalMCPTestCapability | None = None) -> None:
        raw = Path(workspace).expanduser()
        self._workspace_reparse = _reparse_point(raw)
        try:
            self.workspace = _canonical(raw)
        except ExternalMCPNativeError:
            self.workspace = raw.absolute()
        self.config_path = self.workspace / CONFIG_RELATIVE_PATH
        self._test_capability: ExternalMCPTestCapability | None = None
        if test_capability is not None:
            if not isinstance(test_capability, ExternalMCPTestCapability) or test_capability.token is not _TEST_CAPABILITY:
                raise ExternalMCPNativeError("external_mcp_test_capability_required")
            self._test_capability = test_capability

    # ---- stable envelopes -------------------------------------------------
    def _ready(self, **payload: Any) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": True, "status": "READY", "service": self.service_name}
        result.update(payload)
        return result

    def _no_source(self, **payload: Any) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": True, "status": "NO_SOURCE", "service": self.service_name}
        result.update(payload)
        return result

    def _error(self, code: str) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "BLOCKED",
            "service": self.service_name,
            "code": str(code),
            "error": str(code),
        }

    # ---- trusted boundary -------------------------------------------------
    def _authority(self, context: Any) -> Any:
        try:
            authority = resolve_native_transport_context(context)
        except NativeContextError as exc:
            raise ExternalMCPNativeError("trusted_context_capability_required") from exc
        try:
            bound_workspace = _canonical(Path(authority.workspace_id))
        except ExternalMCPNativeError as exc:
            raise ExternalMCPNativeError("context_workspace_mismatch") from exc
        if self._workspace_reparse or _reparse_point(Path(authority.workspace_id)):
            raise ExternalMCPNativeError("reparse_point_blocked")
        if bound_workspace != self.workspace:
            raise ExternalMCPNativeError("context_workspace_mismatch")
        if not _text(authority.agent_instance_id) or not _text(authority.share_group_id):
            raise ExternalMCPNativeError("context_scope_required")
        return authority

    @staticmethod
    def _payload(payload: Any) -> dict[str, Any]:
        if payload is None:
            return {}
        if not isinstance(payload, Mapping):
            raise ExternalMCPNativeError("external_mcp_payload_invalid")
        return {str(key): value for key, value in payload.items()}

    def _guard_payload(self, payload: Mapping[str, Any], authority: Any) -> None:
        for key, value in payload.items():
            lowered = key.casefold()
            if lowered in _PAYLOAD_PATH_KEYS and value not in (None, "", False):
                raise ExternalMCPNativeError("external_mcp_path_forbidden")
            if lowered in _PAYLOAD_IDENTITY_KEYS and value in (None, "", False):
                continue
            if lowered in {"workspace_id", "workspace"}:
                try:
                    if _canonical(Path(value)) != self.workspace:
                        raise ExternalMCPNativeError("context_identity_spoof")
                except (TypeError, ValueError, ExternalMCPNativeError) as exc:
                    if isinstance(exc, ExternalMCPNativeError):
                        raise
                    raise ExternalMCPNativeError("context_identity_spoof") from exc
            elif lowered in {"agent", "agent_id", "agent_instance_id", "trusted_agent_id"} and _text(value) != authority.agent_instance_id:
                raise ExternalMCPNativeError("context_identity_spoof")
            elif lowered in {"share_group_id", "group", "group_id"} and _text(value) != authority.share_group_id:
                raise ExternalMCPNativeError("context_identity_spoof")
            elif lowered in {"project", "project_id", "project_ref"} and _text(value) != authority.project_ref:
                raise ExternalMCPNativeError("context_identity_spoof")
            elif lowered in {"provider", "runtime", "runtime_role"} and _text(value) != getattr(authority, lowered if lowered != "runtime" else "runtime_role"):
                raise ExternalMCPNativeError("context_identity_spoof")

    # ---- config reading ---------------------------------------------------
    def _protected_config_read(self) -> tuple[str, Any, str]:
        """Read config bytes through one protected fd and discard on drift.

        Path checks are intentionally adjacent to ``os.open``/``os.read``.
        ``O_NOFOLLOW`` is used where available; descriptor identity and every
        path component are rechecked before and after the read.  This narrows
        (but cannot mathematically eliminate) an OS-level race window.
        """

        if self._workspace_reparse:
            return "BLOCKED", None, "reparse_point_blocked"
        try:
            before, present = _snapshot_path_chain(self.workspace, self.config_path)
        except ExternalMCPNativeError as exc:
            return "BLOCKED", None, exc.code
        if not present:
            return "NO_SOURCE", None, "no_source"

        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= int(os.O_CLOEXEC)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= int(os.O_NOFOLLOW)
        fd: int | None = None
        try:
            try:
                fd = os.open(self.config_path, flags)
            except FileNotFoundError:
                try:
                    after_missing, still_present = _snapshot_path_chain(self.workspace, self.config_path)
                except ExternalMCPNativeError as exc:
                    return "BLOCKED", None, "external_mcp_path_changed" if exc.code == "reparse_point_blocked" else exc.code
                if not still_present and _same_snapshot(before, after_missing):
                    return "NO_SOURCE", None, "no_source"
                return "BLOCKED", None, "external_mcp_path_changed"
            except OSError:
                try:
                    after_open_error, present_after_open_error = _snapshot_path_chain(self.workspace, self.config_path)
                except ExternalMCPNativeError as exc:
                    return "BLOCKED", None, "external_mcp_path_changed" if exc.code == "reparse_point_blocked" else exc.code
                if not present_after_open_error or not _same_snapshot(before, after_open_error):
                    return "BLOCKED", None, "external_mcp_path_changed"
                return "BLOCKED", None, "invalid_external_mcp_config"

            try:
                fd_before = os.fstat(fd)
                if not stat.S_ISREG(getattr(fd_before, "st_mode", 0)):
                    return "BLOCKED", None, "invalid_external_mcp_config"
                if int(getattr(fd_before, "st_size", 0) or 0) > MAX_CONFIG_BYTES:
                    return "BLOCKED", None, "external_mcp_config_too_large"

                try:
                    path_stat = os.stat(self.config_path, follow_symlinks=False)
                except OSError:
                    return "BLOCKED", None, "external_mcp_path_changed"
                if _is_reparse_info(path_stat) or not _same_object(fd_before, path_stat):
                    return "BLOCKED", None, "external_mcp_path_changed"

                try:
                    after_open, present_after_open = _snapshot_path_chain(self.workspace, self.config_path)
                except ExternalMCPNativeError:
                    return "BLOCKED", None, "external_mcp_path_changed"
                if not present_after_open or not _same_snapshot(before, after_open):
                    return "BLOCKED", None, "external_mcp_path_changed"

                chunks: list[bytes] = []
                total = 0
                while True:
                    remaining = MAX_CONFIG_BYTES - total + 1
                    chunk = os.read(fd, min(64 * 1024, remaining))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_CONFIG_BYTES:
                        return "BLOCKED", None, "external_mcp_config_too_large"
                    chunks.append(chunk)

                fd_after = os.fstat(fd)
                if not _same_object(fd_before, fd_after):
                    return "BLOCKED", None, "external_mcp_path_changed"
                if (
                    getattr(fd_before, "st_size", None) != getattr(fd_after, "st_size", None)
                    or getattr(fd_before, "st_mtime_ns", getattr(fd_before, "st_mtime", None))
                    != getattr(fd_after, "st_mtime_ns", getattr(fd_after, "st_mtime", None))
                ):
                    return "BLOCKED", None, "external_mcp_path_changed"
                try:
                    after_read, present_after_read = _snapshot_path_chain(self.workspace, self.config_path)
                except ExternalMCPNativeError:
                    return "BLOCKED", None, "external_mcp_path_changed"
                if not present_after_read or not _same_snapshot(before, after_read):
                    return "BLOCKED", None, "external_mcp_path_changed"
                try:
                    raw = b"".join(chunks).decode("utf-8")
                except UnicodeError:
                    return "BLOCKED", None, "invalid_external_mcp_config"
                try:
                    return "READY", json.loads(raw), ""
                except json.JSONDecodeError:
                    return "BLOCKED", None, "invalid_external_mcp_config"
            except OSError:
                return "BLOCKED", None, "invalid_external_mcp_config"
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _read_source(self) -> tuple[str, list[Mapping[str, Any]], str]:
        """Return ``(status, servers, code)`` without creating anything."""

        if self._test_capability is not None:
            cap = self._test_capability
            if cap.reader is not None:
                try:
                    parameters = inspect.signature(cap.reader).parameters.values()
                    positional = [p for p in parameters if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
                except (TypeError, ValueError):
                    positional = []
                try:
                    raw = cap.reader(self.workspace) if positional else cap.reader()
                except Exception as exc:
                    raise ExternalMCPNativeError("external_mcp_source_unavailable") from exc
            else:
                raw = {"servers": list(cap.servers)}
            try:
                return "READY", self._parse_root(raw), ""
            except ExternalMCPNativeError as exc:
                return "BLOCKED", [], exc.code

        status, raw, code = self._protected_config_read()
        if status != "READY":
            return status, [], code
        try:
            return "READY", self._parse_root(raw), ""
        except ExternalMCPNativeError as exc:
            return "BLOCKED", [], exc.code

    def _parse_root(self, raw: Any) -> list[Mapping[str, Any]]:
        root = _ensure_mapping(raw, "invalid_external_mcp_config")
        _check_keys(root, _ROOT_KEYS)
        if "schema_version" in root:
            version = root["schema_version"]
            if type(version) is not int:
                raise ExternalMCPNativeError("unknown_external_mcp_schema")
            if version > 1:
                raise ExternalMCPNativeError("future_external_mcp_schema")
            if version < 1:
                raise ExternalMCPNativeError("invalid_external_mcp_schema")
        if "schema" in root and root["schema"] not in ("external-mcp-v1", "external_mcp_v1"):
            raise ExternalMCPNativeError("unknown_external_mcp_schema")
        values = root.get("servers")
        if not isinstance(values, list):
            raise ExternalMCPNativeError("invalid_external_mcp_servers")
        if len(values) > MAX_SERVERS:
            raise ExternalMCPNativeError("external_mcp_server_limit")
        return [self._validate_server(item) for item in values]

    def _validate_server(self, raw: Any) -> Mapping[str, Any]:
        server = _ensure_mapping(raw, "invalid_external_mcp_server")
        _check_keys(server, _SERVER_KEYS)
        # Retain a plain dict only after checking all nested shapes.  This
        # means an unknown/future field blocks the complete config, never just
        # one potentially sensitive server record.
        identity = server.get("server_id", server.get("server_ref", server.get("name", server.get("display_name", ""))))
        identity = _validate_scalar_text(identity, "invalid_external_mcp_server")
        if not identity.strip() or len(identity) > 256:
            raise ExternalMCPNativeError("invalid_external_mcp_server")
        if "descriptor" in server:
            descriptor = _ensure_mapping(server["descriptor"], "invalid_external_mcp_descriptor")
            _check_keys(descriptor, _DESCRIPTOR_KEYS)
            self._validate_descriptor_fields(descriptor)
        self._validate_descriptor_fields(server)
        for key in ("provider", "type", "kind", "display_name", "name", "server_id", "server_ref", "level", "import_strategy", "detected_at", "share_group_id", "project_ref", "agent_instance_id"):
            if key in server and server[key] is not None and not isinstance(server[key], str):
                raise ExternalMCPNativeError("invalid_external_mcp_server")
        if "scope" in server:
            scope = server["scope"]
            if not isinstance(scope, Mapping):
                raise ExternalMCPNativeError("invalid_external_mcp_scope")
            _check_keys(scope, frozenset({"share_group_id", "project_ref", "agent_instance_id"}))
            if any(key in scope and scope[key] is not None and not isinstance(scope[key], str) for key in scope):
                raise ExternalMCPNativeError("invalid_external_mcp_scope")
        for key in ("safe_to_auto_call_tools",):
            if key in server and type(server[key]) is not bool:
                raise ExternalMCPNativeError("invalid_external_mcp_server")
        return dict(server)

    def _validate_descriptor_fields(self, value: Mapping[str, Any]) -> None:
        for key in ("tools", "resources", "memory_entries"):
            if key not in value:
                continue
            entries = value[key]
            if not isinstance(entries, list) or len(entries) > MAX_ITEMS:
                raise ExternalMCPNativeError("invalid_external_mcp_capabilities")
            for item in entries:
                if key == "memory_entries":
                    self._validate_memory(item)
                elif key == "tools":
                    self._validate_tool(item)
                else:
                    self._validate_resource(item)
        capabilities = value.get("capabilities")
        if capabilities is not None:
            caps = _ensure_mapping(capabilities, "invalid_external_mcp_capabilities")
            _check_keys(caps, _CAPABILITY_KEYS)
            for key in caps:
                if key == "known_memory" and type(caps[key]) is not bool:
                    raise ExternalMCPNativeError("invalid_external_mcp_capabilities")
                if key != "known_memory":
                    value = caps[key]
                    if type(value) is int:
                        if value < 0:
                            raise ExternalMCPNativeError("invalid_external_mcp_capabilities")
                    elif isinstance(value, list):
                        if len(value) > MAX_ITEMS or any(
                            not isinstance(item, (str, int, bool)) for item in value
                        ):
                            raise ExternalMCPNativeError("invalid_external_mcp_capabilities")
                    else:
                        raise ExternalMCPNativeError("invalid_external_mcp_capabilities")

    def _validate_tool(self, item: Any) -> None:
        if isinstance(item, str):
            _validate_scalar_text(item)
            return
        tool = _ensure_mapping(item, "invalid_external_mcp_tool")
        _check_keys(tool, _TOOL_KEYS)
        for key in ("name", "title", "description"):
            if key in tool:
                _validate_scalar_text(tool[key], "invalid_external_mcp_tool")
        if "inputSchema" in tool:
            _validate_input_schema(tool["inputSchema"])

    def _validate_resource(self, item: Any) -> None:
        if isinstance(item, str):
            _validate_scalar_text(item)
            return
        resource = _ensure_mapping(item, "invalid_external_mcp_resource")
        _check_keys(resource, _RESOURCE_KEYS)
        for key in ("name", "uri", "mimeType", "description", "text", "content"):
            if key in resource and not isinstance(resource[key], str):
                raise ExternalMCPNativeError("invalid_external_mcp_resource")
            if key in resource and len(resource[key].encode("utf-8", "replace")) > MAX_TEXT_BYTES:
                raise ExternalMCPNativeError("external_mcp_content_too_large")

    def _validate_memory(self, item: Any) -> None:
        memory = _ensure_mapping(item, "invalid_external_mcp_memory_entry")
        _check_keys(memory, _MEMORY_KEYS)
        if not isinstance(memory.get("body", ""), str):
            raise ExternalMCPNativeError("invalid_external_mcp_memory_entry")
        if len(memory.get("body", "").encode("utf-8", "replace")) > MAX_TEXT_BYTES:
            raise ExternalMCPNativeError("external_mcp_content_too_large")
        if "metadata" in memory:
            metadata = _ensure_mapping(memory["metadata"], "invalid_external_mcp_metadata")
            # Metadata is never returned, but unknown/secret fields still make
            # the source ambiguous, so reject the whole config.
            for key in metadata:
                if _is_secret_key(key) or not isinstance(key, str):
                    raise ExternalMCPNativeError("external_mcp_secret_field")

    # ---- semantic records -------------------------------------------------
    @staticmethod
    def _level(raw: Mapping[str, Any], descriptor: Mapping[str, Any]) -> str:
        explicit = raw.get("level", descriptor.get("level"))
        if explicit is not None:
            level = _validate_scalar_text(explicit, "invalid_external_mcp_level")
            if level not in _KNOWN_LEVELS:
                raise ExternalMCPNativeError("unknown_external_mcp_level")
            return level
        tools = descriptor.get("tools", raw.get("tools", [])) or []
        resources = descriptor.get("resources", raw.get("resources", [])) or []
        memory_entries = descriptor.get("memory_entries", raw.get("memory_entries", [])) or []
        names: list[str] = []
        for item in tools:
            names.append(item if isinstance(item, str) else _text(item.get("name", "")))
        lowered = [name.casefold() for name in names]
        name = _text(descriptor.get("name", descriptor.get("display_name", raw.get("name", raw.get("display_name", ""))))).casefold()
        if "memoryguard" in name or any(item.startswith("memoryguard_memory_") for item in lowered):
            return "L4_memoryguard_mcp"
        if memory_entries or any("memory" in item and any(k in item for k in ("read", "search", "write", "list")) for item in lowered):
            return "L3_known_memory_mcp"
        if resources:
            return "L2_generic_resources"
        if tools:
            return "L1_unknown_tools"
        return "L0_unrecognizable"

    def _record(self, raw: Mapping[str, Any]) -> _ServerRecord:
        descriptor = raw.get("descriptor")
        if descriptor is not None:
            descriptor = _ensure_mapping(descriptor, "invalid_external_mcp_descriptor")
        else:
            descriptor = raw
        server_id = _text(raw.get("server_id", raw.get("server_ref", raw.get("name", raw.get("display_name", "")))))
        provider = _safe_label(raw.get("provider", descriptor.get("provider", "unknown")))
        kind = _safe_label(raw.get("type", raw.get("kind", descriptor.get("type", descriptor.get("kind", "mcp")))))
        tools = tuple(descriptor.get("tools", raw.get("tools", [])) or [])
        resources = tuple(descriptor.get("resources", raw.get("resources", [])) or [])
        memories = tuple(descriptor.get("memory_entries", raw.get("memory_entries", [])) or [])
        scope = raw.get("scope")
        share_group_id = _text(raw.get("share_group_id", ""))
        project_ref = _text(raw.get("project_ref", ""))
        if isinstance(scope, Mapping):
            if _is_secret_key("scope"):
                raise ExternalMCPNativeError("external_mcp_secret_field")
            share_group_id = share_group_id or _text(scope.get("share_group_id", ""))
            project_ref = project_ref or _text(scope.get("project_ref", ""))
        return _ServerRecord(
            server_id=server_id,
            server_ref=_server_ref(server_id),
            provider=provider,
            type=kind,
            level=self._level(raw, descriptor),
            tools=tools,
            resources=resources,
            memory_entries=memories,
            share_group_id=share_group_id,
            project_ref=project_ref,
            display_name=_safe_label(raw.get("display_name", descriptor.get("display_name", "")), ""),
        )

    @staticmethod
    def _visible(record: _ServerRecord, authority: Any) -> bool:
        if record.share_group_id and record.share_group_id != authority.share_group_id and not bool(authority.admin):
            return False
        if record.project_ref and record.project_ref != authority.project_ref and not bool(authority.admin):
            return False
        return True

    @staticmethod
    def _summary(record: _ServerRecord) -> dict[str, Any]:
        return {
            "server_ref": record.server_ref,
            "provider": record.provider,
            "type": record.type,
            "level": record.level,
            "display_name": record.display_name,
            "capabilities": {
                "tool_count": len(record.tools),
                "resource_count": len(record.resources),
                "memory_entry_count": len(record.memory_entries),
                "known_memory": record.level in {"L3_known_memory_mcp", "L4_memoryguard_mcp"},
                "unknown_tools_called": False,
            },
        }

    def _records(self, authority: Any) -> tuple[str, list[_ServerRecord], str]:
        status, values, code = self._read_source()
        if status != "READY":
            return status, [], code
        try:
            records = [self._record(value) for value in values]
        except ExternalMCPNativeError as exc:
            return "BLOCKED", [], exc.code
        records = [record for record in records if self._visible(record, authority)]
        records.sort(key=lambda item: item.server_ref)
        return "READY", records, ""

    @staticmethod
    def _lookup(records: list[_ServerRecord], query: str) -> _ServerRecord | None:
        query = _text(query)
        if not query:
            return None
        for record in records:
            if query in {record.server_ref, record.server_id}:
                return record
        return None

    # ---- public read operations ------------------------------------------
    def list_external_mcp_servers(self, payload: Any = None, *, context: Any = None, **_: Any) -> dict[str, Any]:
        try:
            authority = self._authority(context)
            body = self._payload(payload)
            self._guard_payload(body, authority)
            status, records, code = self._records(authority)
            if status == "NO_SOURCE":
                return self._no_source(servers=[], total=0)
            if status != "READY":
                return self._error(code or "external_mcp_source_unavailable")
            return self._ready(servers=[self._summary(record) for record in records], total=len(records))
        except ExternalMCPNativeError as exc:
            return self._error(exc.code)

    memoryguard_external_mcp_list = list_external_mcp_servers
    # Detector-compatible names are read-only aliases.  They intentionally do
    # not expose ``_ensure_dirs``/``_upsert_server`` or any import operation.
    list_servers = list_external_mcp_servers

    def detect_external_mcp(
        self,
        server_id: str,
        descriptor: Mapping[str, Any] | None = None,
        *,
        context: Any = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Classify one server without persisting the supplied descriptor."""

        try:
            authority = self._authority(context)
            body = self._payload(descriptor) if descriptor is not None else {}
            self._guard_payload({}, authority)
            if descriptor is not None:
                # Validate as a descriptor, not as a persisted server envelope.
                _check_keys(body, _DESCRIPTOR_KEYS)
                self._validate_descriptor_fields(body)
                raw: Mapping[str, Any] = {"server_id": _validate_scalar_text(server_id), "descriptor": body}
                record = self._record(raw)
            else:
                status, records, code = self._records(authority)
                if status == "NO_SOURCE":
                    return self._no_source(server_ref=_server_ref(_text(server_id)), found=False)
                if status != "READY":
                    return self._error(code or "external_mcp_source_unavailable")
                record = self._lookup(records, server_id)
                if record is None:
                    return self._no_source(server_ref=_server_ref(_text(server_id)), found=False)
            result = self._summary(record)
            result["found"] = True
            result["safe_to_auto_call_tools"] = False
            return self._ready(**result)
        except ExternalMCPNativeError as exc:
            return self._error(exc.code)

    def preview_external_mcp_import(
        self,
        server_id: str,
        descriptor: Mapping[str, Any] | None = None,
        *,
        context: Any = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Return content digests/lengths only; raw body/content never crosses transport."""

        try:
            authority = self._authority(context)
            if descriptor is not None:
                desc = self._payload(descriptor)
                _check_keys(desc, _DESCRIPTOR_KEYS)
                self._validate_descriptor_fields(desc)
                record = self._record({"server_id": _validate_scalar_text(server_id), "descriptor": desc})
            else:
                status, records, code = self._records(authority)
                if status == "NO_SOURCE":
                    return self._no_source(server_ref=_server_ref(_text(server_id)), preview_entries=[], total=0, found=False)
                if status != "READY":
                    return self._error(code or "external_mcp_source_unavailable")
                record = self._lookup(records, server_id)
                if record is None:
                    return self._no_source(server_ref=_server_ref(_text(server_id)), preview_entries=[], total=0, found=False)
            entries: list[dict[str, Any]] = []
            if record.level in {"L3_known_memory_mcp", "L4_memoryguard_mcp"}:
                for index, item in enumerate(record.memory_entries):
                    content = _text(item.get("body", ""))
                    if not content:
                        continue
                    digest = _digest(content)
                    entries.append({
                        "entry_ref": _entry_ref(record.server_ref, "memory", index, digest),
                        "kind": "memory",
                        "content_digest": digest,
                        "content_length": len(content),
                        "source": "provided_memory_entry",
                    })
            if record.level == "L2_generic_resources":
                for index, item in enumerate(record.resources):
                    if isinstance(item, str):
                        content = item
                    else:
                        content = _text(item.get("text", item.get("content", "")))
                    if not content:
                        continue
                    digest = _digest(content)
                    entries.append({
                        "entry_ref": _entry_ref(record.server_ref, "resource", index, digest),
                        "kind": "resource",
                        "content_digest": digest,
                        "content_length": len(content),
                        "source": "provided_resource_content",
                    })
            return self._ready(
                server_ref=record.server_ref,
                level=record.level,
                unknown_tools_called=False,
                preview_entries=entries,
                total=len(entries),
                import_strategy={
                    "L4_memoryguard_mcp": "direct_sync_or_merge",
                    "L3_known_memory_mcp": "readonly_preview_then_import",
                    "L2_generic_resources": "user_selected_resources_then_import",
                    "L1_unknown_tools": "detect_only_unknown_tools_not_called",
                    "L0_unrecognizable": "ask_user_to_export_md_or_json",
                }[record.level],
            )
        except ExternalMCPNativeError as exc:
            return self._error(exc.code)

    def _persist_servers(self, servers: list[Mapping[str, Any]]) -> None:
        """Atomically replace the validated static descriptor registry."""
        if self._test_capability is not None:
            raise ExternalMCPNativeError("external_mcp_test_write_forbidden")
        if self._workspace_reparse or _reparse_point(self.workspace):
            raise ExternalMCPNativeError("reparse_point_blocked")
        parent = self.config_path.parent
        if _reparse_point(parent) or _reparse_point(self.config_path):
            raise ExternalMCPNativeError("reparse_point_blocked")
        parent.mkdir(parents=True, exist_ok=True)
        if _reparse_point(parent):
            raise ExternalMCPNativeError("reparse_point_blocked")
        root = {"schema": "external-mcp-v1", "schema_version": 1, "servers": [dict(item) for item in servers]}
        # Validate exactly what will be persisted before opening a write handle.
        self._parse_root(root)
        encoded = json.dumps(root, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        if len(encoded) > MAX_CONFIG_BYTES:
            raise ExternalMCPNativeError("external_mcp_config_too_large")
        temp = self.config_path.with_name(f".{self.config_path.name}.tmp-{os.getpid()}-{_digest(encoded)[:10]}")
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_CLOEXEC"):
                flags |= int(os.O_CLOEXEC)
            fd = os.open(temp, flags, 0o600)
            try:
                written = 0
                while written < len(encoded):
                    count = os.write(fd, encoded[written:])
                    if count <= 0:
                        raise ExternalMCPNativeError("external_mcp_write_failed")
                    written += count
                os.fsync(fd)
            finally:
                os.close(fd)
            if _reparse_point(temp) or _reparse_point(self.config_path):
                raise ExternalMCPNativeError("reparse_point_blocked")
            os.replace(temp, self.config_path)
        except ExternalMCPNativeError:
            raise
        except OSError as exc:
            raise ExternalMCPNativeError("external_mcp_write_failed") from exc
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def import_external_mcp(self, payload: Any = None, *, context: Any = None, **_: Any) -> dict[str, Any]:
        """Validate/classify and persist one descriptor without invoking tools."""
        try:
            authority = self._authority(context)
            body = self._payload(payload)
            self._guard_payload(body, authority)
            raw_descriptor = body.get("descriptor_json", body.get("descriptor"))
            if isinstance(raw_descriptor, str):
                try:
                    descriptor = json.loads(raw_descriptor)
                except json.JSONDecodeError as exc:
                    raise ExternalMCPNativeError("invalid_external_mcp_descriptor") from exc
            else:
                descriptor = raw_descriptor
            descriptor = _ensure_mapping(descriptor, "invalid_external_mcp_descriptor")
            _check_keys(descriptor, _DESCRIPTOR_KEYS)
            self._validate_descriptor_fields(descriptor)
            server_id = _text(body.get("server_id") or descriptor.get("name") or descriptor.get("display_name"))
            if not server_id:
                raise ExternalMCPNativeError("invalid_external_mcp_server")
            server_id = _validate_scalar_text(server_id, "invalid_external_mcp_server")
            record_raw: dict[str, Any] = {
                "server_id": server_id,
                "descriptor": dict(descriptor),
                "share_group_id": authority.share_group_id,
                "project_ref": authority.project_ref,
                "agent_instance_id": authority.agent_instance_id,
                "safe_to_auto_call_tools": False,
            }
            record = self._record(record_raw)
            # Read/validate the old registry under the same workspace lock so a
            # concurrent import cannot silently lose another descriptor.
            with WorkspaceGovernanceLock(self.workspace):
                status, values, code = self._read_source()
                if status == "NO_SOURCE":
                    values = []
                elif status != "READY":
                    raise ExternalMCPNativeError(code or "external_mcp_source_unavailable")
                existed = any(
                    _text(item.get("server_id", item.get("server_ref", ""))) == server_id
                    for item in values
                )
                updated = [dict(item) for item in values if _text(item.get("server_id", item.get("server_ref", ""))) != server_id]
                updated.append(record_raw)
                if len(updated) > MAX_SERVERS:
                    raise ExternalMCPNativeError("external_mcp_server_limit")
                self._persist_servers(updated)
            summary = self._summary(record)
            return self._ready(
                imported=True,
                updated_existing=existed,
                safe_to_auto_call_tools=False,
                unknown_tools_called=False,
                **summary,
            )
        except ExternalMCPNativeError as exc:
            return self._error(exc.code)

    # Compatibility aliases for callers that already know the detector API.
    detect_server = detect_external_mcp
    preview_import = preview_external_mcp_import
    import_descriptor = import_external_mcp

    # ---- operation dispatch ----------------------------------------------
    def dispatch(self, name: str, payload: Any = None, *, context: Any = None, **kwargs: Any) -> dict[str, Any]:
        operation = _text(name)
        try:
            body = self._payload(payload)
        except ExternalMCPNativeError as exc:
            return self._error(exc.code)
        if operation in {"memoryguard_external_mcp_list", "list_external_mcp_servers"}:
            return self.list_external_mcp_servers(body, context=context, **kwargs)
        if operation in {"detect_external_mcp"}:
            return self.detect_external_mcp(body.get("server_ref", body.get("server_id", "")), body.get("descriptor"), context=context, **kwargs)
        if operation in {"preview_external_mcp_import"}:
            return self.preview_external_mcp_import(body.get("server_ref", body.get("server_id", "")), body.get("descriptor"), context=context, **kwargs)
        if operation in {"memoryguard_external_mcp_import", "import_external_mcp", "import_descriptor"}:
            return self.import_external_mcp(body, context=context, **kwargs)
        return self._error("unknown_external_mcp_operation")

    call = dispatch


# Naming aliases keep parent/native-port wiring free to use either convention.
ExternalMCPNativeService = NativeExternalMCPService
NativeExternalMCPReadService = NativeExternalMCPService

__all__ = [
    "CONFIG_RELATIVE_PATH", "ExternalMCPNativeError", "ExternalMCPNativeService",
    "ExternalMCPTestCapability", "NativeExternalMCPReadService", "NativeExternalMCPService",
    "bind_external_mcp_test_capability",
]
