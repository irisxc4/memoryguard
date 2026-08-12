"""Read-only V2 service adapters with a fail-closed boundary.

The legacy source registry is intentionally not constructed here: its
constructor creates ``.memoryguard`` and may persist a default source.  These
services read existing configuration only and are dependency-injected so the
native port can bind them without importing MCP/GUI code.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import inspect
import json
import os
from pathlib import Path
import stat
import time
from typing import Any, Callable, Mapping

from .source_native import (
    INSTRUCTION_FILES,
    META_EXTS,
    TEXT_EXTS,
    TEXT_MEDIA_TYPES,
    ScanBudget,
    make_adapter,
)


_IDENTITY_KEYS = frozenset(
    {
        "workspace", "workspace_id", "agent", "agent_id", "agent_instance_id",
        "trusted_agent_id", "share_group_id", "group", "group_id",
        "project", "project_id", "project_ref", "provider", "runtime",
        "runtime_role", "trusted_context", "trusted_identity", "identity",
        "session_id", "session_source", "session_trusted", "context_hash",
        "admin", "is_admin", "authority", "capability", "capabilities",
    }
)
_SECRET_WORDS = (
    "secret", "token", "password", "passwd", "credential", "private_key",
    "authorization", "cookie", "api_key", "access_key", "refresh_key",
)
_PATH_WORDS = (
    "path", "workspace", "command", "cmd", "argv", "args", "cwd", "directory",
    "database", "lease_file", "control_workspace",
)
_REPARSE_ATTRIBUTE = 0x0400  # FILE_ATTRIBUTE_REPARSE_POINT on Windows
_DEFAULT_MAX_FILES = 1_000
_DEFAULT_MAX_TOTAL_SIZE = 100 * 1024 * 1024
_DEFAULT_MAX_SINGLE_FILE = 10 * 1024 * 1024
_DEFAULT_MAX_DEPTH = 20
_DEFAULT_TIMEOUT_SECONDS = 30


class SafeServiceError(RuntimeError):
    """Stable error code; details are deliberately not returned to callers."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "safe_service_error")
        super().__init__(self.code)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _safe_marker(value: Any, default: str = "UNKNOWN") -> str:
    text = _text(value)
    if not text or Path(text).is_absolute() or text.startswith("\\\\"):
        return default
    if any(word in text.casefold() for word in _SECRET_WORDS + ("\n", "\r")):
        return default
    return text[:128]


def _safe_label(value: Any, default: str = "") -> str:
    text = _text(value)
    if not text or Path(text).is_absolute() or text.startswith("\\\\"):
        return default
    if ":\\" in text or ":/" in text or any(word in text.casefold() for word in _SECRET_WORDS):
        return default
    return text[:256]


def _count(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            result = to_dict()
        except Exception:  # noqa: BLE001 - context is untrusted at this seam
            result = {}
        return dict(result) if isinstance(result, Mapping) else {}
    result: dict[str, Any] = {}
    for key in (
        "is_admin", "admin", "trusted_agent_id", "agent_instance_id", "share_group_id",
        "workspace_id", "project_ref", "provider", "runtime_role",
    ):
        if hasattr(value, key):
            result[key] = getattr(value, key)
    return result


def _request_mapping(value: Any) -> dict[str, Any]:
    """Accept the native payload mapping and the convenient direct path form."""
    if isinstance(value, (str, os.PathLike)):
        return {"path": value}
    return _mapping(value)


def _is_admin(context: Any) -> bool:
    """Read admin only from the bound context, never from request payload."""
    data = _mapping(context)
    return bool(data.get("is_admin") is True or data.get("admin") is True)


def _call_provider(provider: Any, workspace: Path | None = None) -> Any:
    if not callable(provider):
        return provider
    try:
        parameters = inspect.signature(provider).parameters.values()
    except (TypeError, ValueError):
        # Signature negotiation itself is best effort, but invocation is not:
        # make one conservative call and let provider failures propagate to
        # the caller's fail-closed boundary.  In particular, do not retry an
        # internal TypeError without the workspace argument.
        return provider()
    positional = [item for item in parameters if item.kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )]
    # Choose the call shape once.  A TypeError raised by provider code must
    # remain visible to RuntimeDiagnosticsService, which converts it to a
    # stable unknown/empty diagnostic rather than accidentally changing the
    # security-sensitive argument list on a retry.
    return provider(workspace) if positional and workspace is not None else provider()


def _safe_json(value: Any, *, include_details: bool = False) -> Any:
    """Keep diagnostics metadata small and remove paths, commands, secrets."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            lower = key.casefold()
            if any(word in lower for word in _SECRET_WORDS):
                continue
            if any(word in lower for word in _PATH_WORDS):
                continue
            if not include_details and key.casefold() in {"pid", "process_id", "started_at"}:
                continue
            result[key] = _safe_json(raw_value, include_details=include_details)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_json(item, include_details=include_details) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        # Avoid accidentally returning a Windows/POSIX absolute path stored in
        # an unlabelled field from an injected status provider.
        if isinstance(value, str) and (
            Path(value).is_absolute() or value.startswith("\\\\")
        ):
            return "<redacted>"
        return value
    return str(value)


def _error(code: str, *, service: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "BLOCKED",
        "service": service,
        "code": str(code),
        "error": str(code),
    }


def _ready(service: str, **payload: Any) -> dict[str, Any]:
    result = {"ok": True, "status": "READY", "service": service}
    result.update(payload)
    return result


def _reparse_point(path: Path) -> bool:
    """Detect symlinks and Windows reparse points without following them."""
    try:
        current = path
        # A missing leaf is not itself a reparse point; existing parents still
        # matter because a junction/symlink can redirect the requested root.
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
        # Inability to inspect path metadata is unsafe and therefore blocked.
        return True
    return False


def _canonical(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except OSError as exc:
        raise SafeServiceError("path_resolution_failed") from exc


def _contained(path: Path, root: Path) -> bool:
    try:
        _canonical(path).relative_to(_canonical(root))
        return True
    except (ValueError, SafeServiceError):
        return False


def _reference(path: Path, *, workspace: Path, root_id: str = "") -> str:
    """Stable non-absolute path reference for a source or candidate."""
    try:
        rel = _canonical(path).relative_to(_canonical(workspace))
        value = str(rel).replace("\\", "/")
        return f"workspace:{value or '.'}"
    except (ValueError, SafeServiceError):
        digest = hashlib.sha256(str(_canonical(path)).encode("utf-8")).hexdigest()[:16]
        safe_root = _safe_label(root_id, "external")
        return f"{safe_root or 'external'}:{digest}"


def _budget(payload: Mapping[str, Any]) -> dict[str, int]:
    """Parse caller budgets with hard ceilings; malformed values fail closed."""
    aliases = {
        "max_files": ("max_files", "file_limit", "max_items"),
        "max_total_size": ("max_total_size", "max_bytes", "total_size_limit"),
        "max_single_file": ("max_single_file", "single_file_limit"),
        "max_depth": ("max_depth", "depth_limit"),
        "timeout_seconds": ("timeout_seconds", "timeout"),
    }
    limits = {
        "max_files": _DEFAULT_MAX_FILES,
        "max_total_size": _DEFAULT_MAX_TOTAL_SIZE,
        "max_single_file": _DEFAULT_MAX_SINGLE_FILE,
        "max_depth": _DEFAULT_MAX_DEPTH,
        "timeout_seconds": _DEFAULT_TIMEOUT_SECONDS,
    }
    for name, keys in aliases.items():
        supplied = next((payload[key] for key in keys if key in payload), None)
        if supplied is None:
            continue
        if isinstance(supplied, bool):
            raise SafeServiceError("invalid_budget")
        try:
            number = int(supplied)
        except (TypeError, ValueError) as exc:
            raise SafeServiceError("invalid_budget") from exc
        if number < 0:
            raise SafeServiceError("invalid_budget")
        limits[name] = min(number, limits[name])
    return limits


def _bounded_candidates(
    adapter: Any,
    target: Path,
    limits: Mapping[str, int],
) -> tuple[list[tuple[Path, int]], int, float]:
    """Inventory once, then enforce the same hard limits for every caller.

    ``SelectedFileAdapter`` is intentionally bypassed for no directory walk,
    so its single-file path must still pass max-files/size/timeout checks here.
    Directory adapters provide truncation entries, but their returned paths are
    re-checked as an untrusted result before a caller can expose or hash them.
    """
    deadline = time.monotonic() + limits["timeout_seconds"]
    if target.is_file():
        candidates: list[Path] = [target] if limits["max_files"] > 0 else []
        truncation: list[Any] = [] if candidates else [target]
    else:
        candidates, truncation = adapter.inventory(
            target,
            # The adapter has its own walk guard; the post-filter below keeps
            # the contract identical for selected files and adapter variants.
            _scan_budget(limits),
        )

    accepted: list[tuple[Path, int]] = []
    skipped = len(truncation)
    total_size = 0
    for index, candidate in enumerate(candidates):
        if time.monotonic() >= deadline:
            skipped += len(candidates) - index
            break
        if len(accepted) >= limits["max_files"]:
            skipped += len(candidates) - index
            break
        if _reparse_point(candidate) or not _contained(candidate, target):
            skipped += 1
            continue
        try:
            size = int(candidate.stat().st_size)
        except OSError:
            skipped += 1
            continue
        if time.monotonic() >= deadline:
            skipped += len(candidates) - index
            break
        if size > limits["max_single_file"]:
            skipped += 1
            continue
        if total_size + size > limits["max_total_size"]:
            skipped += len(candidates) - index
            break
        accepted.append((candidate, size))
        total_size += size
    return accepted, skipped, deadline


def _scan_budget(limits: Mapping[str, int]) -> ScanBudget:
    """Build the native adapter budget from the caller's bounded limits."""
    return ScanBudget(
        max_files=limits["max_files"],
        max_total_size=limits["max_total_size"],
        max_single_file=limits["max_single_file"],
        max_depth=limits["max_depth"],
        timeout_seconds=limits["timeout_seconds"],
    )


def _sha256_path(path: Path, deadline: float) -> str:
    """Hash in bounded chunks and fail closed when the scan deadline expires."""
    if time.monotonic() >= deadline:
        raise SafeServiceError("scan_timeout")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            if time.monotonic() >= deadline:
                raise SafeServiceError("scan_timeout")
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    if time.monotonic() >= deadline:
        raise SafeServiceError("scan_timeout")
    return digest.hexdigest()


_ROOT_KEYS = frozenset(
    {
        "root_id", "type", "display_name", "path", "scope", "authorized_at",
        "recursive", "follow_symlinks", "include", "exclude", "enabled",
        "agent_instance_id", "authorized_agent_ids", "agent_enabled", "surface_id",
        "source_category", "ingestion_policy", "ownership", "target_role",
        "scope_source", "project_ref", "discovery_object_id",
    }
)


class PureSourceReadService:
    """Read existing source configuration without registry side effects."""

    service_name = "source_read"

    def __init__(self, workspace: str | Path) -> None:
        raw_workspace = Path(workspace).expanduser()
        self._workspace_reparse = raw_workspace.exists() and _reparse_point(raw_workspace)
        self.workspace = _canonical(raw_workspace)

    def _load_roots(self) -> tuple[str, list[Any], str]:
        """Return ``(status, roots, code)``; never creates or repairs files."""
        if self._workspace_reparse:
            return "BLOCKED", [], "reparse_point_blocked"
        mg_dir = self.workspace / ".memoryguard"
        # Check the directory and both config filenames even when a symlink is
        # broken; ``Path.exists()`` alone would otherwise turn a reparse path
        # into a misleading NO_SOURCE result.
        if _reparse_point(mg_dir):
            return "BLOCKED", [], "reparse_point_blocked"
        config_paths = [mg_dir / "config.json", mg_dir / "config.local.json"]
        if any(_reparse_point(path) for path in config_paths):
            return "BLOCKED", [], "reparse_point_blocked"
        existing = [path for path in config_paths if path.is_file()]
        if not existing:
            return "NO_SOURCE", [], "no_source"
        try:
            from ..schema_v3 import SourceRoot, SourceRootType
        except Exception as exc:  # pragma: no cover - package import failure
            return "BLOCKED", [], "source_schema_unavailable"
        # SourceRegistry loads shared config first and machine-local config
        # second; the latter replaces an earlier root with the same root_id.
        # Keep that precedence while validating every effective record at this
        # read-only boundary (no repair, default insertion, or file writes).
        roots_by_id: dict[str, Any] = {}
        for path in existing:
            if _reparse_point(path):
                return "BLOCKED", [], "reparse_point_blocked"
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return "BLOCKED", [], "invalid_source_config"
            if not isinstance(raw, Mapping) or set(raw) != {"sources"}:
                return "BLOCKED", [], "unknown_source_config"
            values = raw.get("sources")
            if not isinstance(values, list):
                return "BLOCKED", [], "invalid_source_config"
            for item in values:
                if not isinstance(item, Mapping) or not _ROOT_KEYS.issuperset(item):
                    return "BLOCKED", [], "unknown_source_root_fields"
                required = {"root_id", "type", "display_name", "path"}
                if not required.issubset(item):
                    return "BLOCKED", [], "invalid_source_root"
                if not all(isinstance(item[key], str) and _text(item[key]) for key in required):
                    return "BLOCKED", [], "invalid_source_root"
                # SourceRoot.from_dict is intentionally permissive for legacy
                # data.  The safe service must reject malformed JSON shapes
                # before that parser can coerce attacker-controlled values.
                bool_fields = {"recursive", "follow_symlinks", "enabled"}
                if any(key in item and type(item[key]) is not bool for key in bool_fields):
                    return "BLOCKED", [], "invalid_source_root"
                list_fields = {"include", "exclude", "authorized_agent_ids"}
                if any(
                    key in item
                    and (
                        not isinstance(item[key], list)
                        or any(not isinstance(entry, str) for entry in item[key])
                    )
                    for key in list_fields
                ):
                    return "BLOCKED", [], "invalid_source_root"
                if "agent_enabled" in item:
                    enabled = item["agent_enabled"]
                    if not isinstance(enabled, Mapping) or any(
                        not isinstance(key, str) or type(value) is not bool
                        for key, value in enabled.items()
                    ):
                        return "BLOCKED", [], "invalid_source_root"
                try:
                    root = SourceRoot.from_dict(dict(item))
                    # Force enum validation even if a future parser changes.
                    SourceRootType(root.type.value if hasattr(root.type, "value") else root.type)
                except (AttributeError, KeyError, TypeError, ValueError):
                    return "BLOCKED", [], "unknown_source_root_type"
                roots_by_id[root.root_id] = root
        roots = list(roots_by_id.values())
        return ("READY", roots, "") if roots else ("NO_SOURCE", [], "no_source")

    @staticmethod
    def _authorized_for_context(root: Any, context: Any) -> bool:
        """Apply only trusted bound scope; request identity is never consulted."""
        bound = _mapping(context)
        agent = _text(bound.get("trusted_agent_id") or bound.get("agent_instance_id"))
        authorized = [
            _text(item)
            for item in (getattr(root, "authorized_agent_ids", []) or [])
            if _text(item)
        ]
        if authorized and (not agent or agent not in authorized):
            return False
        enabled = getattr(root, "agent_enabled", {}) or {}
        if agent and agent in enabled and enabled[agent] is False:
            return False
        project_ref = _text(getattr(root, "project_ref", ""))
        bound_project = _text(bound.get("project_ref") or bound.get("project_id"))
        if project_ref and bound_project and project_ref != bound_project:
            return False
        return True

    def _validate_root(self, root: Any) -> tuple[str, Path | None, str]:
        path_value = _text(getattr(root, "path", ""))
        if not path_value:
            return "BLOCKED", None, "invalid_source_root"
        raw = Path(path_value).expanduser()
        if not raw.is_absolute():
            return "BLOCKED", None, "relative_source_path"
        if _reparse_point(raw):
            return "BLOCKED", None, "reparse_point_blocked"
        try:
            resolved = _canonical(raw)
        except SafeServiceError as exc:
            return "BLOCKED", None, exc.code
        root_type = getattr(getattr(root, "type", None), "value", getattr(root, "type", ""))
        if root_type == "project_directory" and not _contained(resolved, self.workspace):
            return "BLOCKED", None, "path_out_of_scope"
        if not resolved.exists():
            return "MISSING", resolved, "root_missing"
        if root_type == "selected_file" and not resolved.is_file():
            return "BLOCKED", None, "source_type_mismatch"
        if root_type != "selected_file" and not resolved.is_dir():
            return "BLOCKED", None, "source_type_mismatch"
        return "READY", resolved, ""

    def _source_item(self, root: Any, state: str, path: Path | None, code: str, *, admin: bool) -> dict[str, Any]:
        root_type = getattr(getattr(root, "type", None), "value", getattr(root, "type", ""))
        item: dict[str, Any] = {
            "root_id": _safe_label(getattr(root, "root_id", ""), "<redacted>"),
            "type": _safe_label(root_type, "unknown"),
            "display_name": _safe_label(getattr(root, "display_name", ""), "<redacted>"),
            "scope": _safe_label(getattr(root, "scope", ""), "unknown"),
            "enabled": bool(getattr(root, "enabled", True)),
            "state": state,
            "reference": _reference(path, workspace=self.workspace, root_id=_text(getattr(root, "root_id", ""))) if path else "",
        }
        if code:
            item["code"] = code
        # Absolute paths are not part of the stable neutral response.  An
        # admin can request a reference that is still redacted to a workspace
        # relative form, never the raw path from config.
        if admin and path is not None:
            item["path_reference"] = _reference(path, workspace=self.workspace, root_id=item["root_id"])
        return item

    def list_sources(self, payload: Any = None, *, context: Any = None, **_: Any) -> dict[str, Any]:
        del payload
        admin = _is_admin(context)
        status, roots, code = self._load_roots()
        if status != "READY":
            if status == "NO_SOURCE":
                return _ready(self.service_name, status="NO_SOURCE", sources=[], total=0)
            return _error(code, service=self.service_name)
        items: list[dict[str, Any]] = []
        for root in roots:
            if not self._authorized_for_context(root, context):
                continue
            state, path, root_code = self._validate_root(root)
            if not bool(getattr(root, "enabled", True)):
                state = "DISABLED"
            items.append(self._source_item(root, state, path, root_code, admin=admin))
        blocked = next((item for item in items if item["state"] == "BLOCKED"), None)
        if blocked is not None:
            result = _error(blocked.get("code") or "source_root_blocked", service=self.service_name)
            result.update(sources=items, total=len(items))
            return result
        if not any(item["state"] == "READY" for item in items):
            return _ready(self.service_name, status="NO_SOURCE", sources=items, total=len(items))
        return _ready(self.service_name, sources=items, total=len(items))

    def _inventory_root(self, root: Any, path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
        if bool(getattr(root, "follow_symlinks", False)):
            return {"state": "BLOCKED", "code": "symlink_traversal_disabled", "candidate_count": 0}
        safe_root = replace(root, follow_symlinks=False)
        try:
            limits = _budget(payload)
            adapter = make_adapter(safe_root)
            accepted, skipped, _ = _bounded_candidates(adapter, path, limits)
        except SafeServiceError as exc:
            return {"state": "BLOCKED", "code": exc.code, "candidate_count": 0}
        except (OSError, ValueError, TypeError):
            return {"state": "BLOCKED", "code": "source_inventory_failed", "candidate_count": 0}
        counts = {
            "candidate_count": 0,
            "readable": 0,
            "unsupported": 0,
            "unreadable": 0,
            "skipped_by_policy": skipped,
            "unaccounted_count": 0,
        }
        references: list[str] = []
        for candidate, _size in accepted:
            counts["candidate_count"] += 1
            references.append(_reference(candidate, workspace=self.workspace, root_id=_text(root.root_id)))
            ext = candidate.suffix.casefold()
            if ext in TEXT_EXTS or ext in META_EXTS or candidate.name in INSTRUCTION_FILES:
                counts["readable"] += 1
            else:
                counts["unsupported"] += 1
        if counts["skipped_by_policy"] or counts["unreadable"] or counts["unsupported"]:
            state = "PARTIAL"
        else:
            state = "READY"
        return {"state": state, **counts, "references": references}

    def scan_summary(self, payload: Any = None, *, context: Any = None, **_: Any) -> dict[str, Any]:
        payload_map = _mapping(payload)
        try:
            # Validate once even when there are no configured roots; malformed
            # caller budgets must never be silently treated as NO_SOURCE.
            _budget(payload_map)
        except SafeServiceError as exc:
            return _error(exc.code, service=self.service_name)
        status, roots, code = self._load_roots()
        if status == "NO_SOURCE":
            return _ready(self.service_name, status="NO_SOURCE", coverage={"candidate_count": 0}, roots=[])
        if status != "READY":
            return _error(code, service=self.service_name)
        root_summaries: list[dict[str, Any]] = []
        totals = {
            "candidate_count": 0, "readable": 0, "unsupported": 0,
            "unreadable": 0, "skipped_by_policy": 0, "unaccounted_count": 0,
        }
        for root in roots:
            if not self._authorized_for_context(root, context):
                continue
            if not bool(getattr(root, "enabled", True)):
                continue
            state, path, root_code = self._validate_root(root)
            if state != "READY" or path is None:
                root_summaries.append({"root_id": _safe_label(root.root_id, "<redacted>"), "state": state, "code": root_code})
                continue
            summary = self._inventory_root(root, path, payload_map)
            item = {"root_id": _safe_label(root.root_id, "<redacted>"), "state": summary.pop("state", "BLOCKED"), **summary}
            root_summaries.append(item)
            for key in totals:
                totals[key] += int(item.get(key, 0) or 0)
        if not root_summaries or all(item.get("state") == "MISSING" for item in root_summaries):
            overall = "NO_SOURCE"
        elif any(item.get("state") == "BLOCKED" for item in root_summaries):
            overall = "BLOCKED"
        elif any(item.get("state") == "PARTIAL" for item in root_summaries):
            overall = "PARTIAL"
        else:
            overall = "READY"
        if overall == "BLOCKED":
            blocked = next((item for item in root_summaries if item.get("state") == "BLOCKED"), {})
            result = _error(blocked.get("code") or "source_scan_blocked", service=self.service_name)
            result.update(coverage=totals, roots=root_summaries)
            return result
        return _ready(self.service_name, status=overall, coverage=totals, roots=root_summaries)

    # Native port aliases keep public MCP names explicit and injectable.
    memoryguard_list_sources = list_sources
    memoryguard_scan_summary = scan_summary

    def dispatch(self, name: str, payload: Any = None, *, context: Any = None, **kwargs: Any) -> dict[str, Any]:
        handlers = {
            "list_sources": self.list_sources,
            "memoryguard_list_sources": self.list_sources,
            "scan_summary": self.scan_summary,
            "memoryguard_scan_summary": self.scan_summary,
        }
        handler = handlers.get(_text(name))
        return handler(payload, context=context, **kwargs) if handler else _error("unknown_source_operation", service=self.service_name)

    call = dispatch


class ImportPreviewService:
    """Detect/inventory/preview only; no staging or target writes."""

    service_name = "import_preview"

    def __init__(self, workspace: str | Path, *, source_reader: PureSourceReadService | None = None) -> None:
        self.workspace = _canonical(Path(workspace))
        self.source_reader = source_reader or PureSourceReadService(self.workspace)

    def _target(self, payload: Mapping[str, Any]) -> Path:
        value = payload.get("path") or payload.get("source_path")
        if not isinstance(value, (str, os.PathLike)) or not _text(value):
            raise SafeServiceError("path_required")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise SafeServiceError("relative_source_path")
        if _reparse_point(path):
            raise SafeServiceError("reparse_point_blocked")
        return _canonical(path)

    def _authorized_root(self, target: Path, context: Any = None) -> tuple[Any, Path]:
        status, roots, code = self.source_reader._load_roots()
        if status == "NO_SOURCE":
            raise SafeServiceError("no_source")
        if status != "READY":
            raise SafeServiceError(code or "source_config_blocked")
        candidates: list[tuple[Any, Path]] = []
        for root in roots:
            if not self.source_reader._authorized_for_context(root, context):
                continue
            if not bool(getattr(root, "enabled", True)):
                continue
            root_state, root_path, root_code = self.source_reader._validate_root(root)
            if root_state != "READY" or root_path is None:
                continue
            root_type = getattr(getattr(root, "type", None), "value", getattr(root, "type", ""))
            if root_type == "selected_file":
                inside = _canonical(target) == root_path
            else:
                inside = _contained(target, root_path)
            if inside:
                candidates.append((root, root_path))
        if not candidates:
            raise SafeServiceError("path_out_of_scope")
        # Prefer the most specific root when nested authorized roots exist.
        candidates.sort(key=lambda item: len(item[1].parts), reverse=True)
        return candidates[0]

    def detect(self, payload: Any = None, *, context: Any = None, **_: Any) -> dict[str, Any]:
        raw = _request_mapping(payload)
        try:
            target = self._target(raw)
            root, root_path = self._authorized_root(target, context)
        except SafeServiceError as exc:
            return _error(exc.code, service=self.service_name)
        root_type = getattr(getattr(root, "type", None), "value", getattr(root, "type", ""))
        return _ready(
            self.service_name,
            detected=True,
            adapter=root_type,
            root_id=_safe_label(root.root_id, "<redacted>"),
            reference=_reference(target, workspace=self.workspace, root_id=_text(root.root_id)),
            root_reference=_reference(root_path, workspace=self.workspace, root_id=_text(root.root_id)),
        )

    def inventory(self, payload: Any = None, *, context: Any = None, **_: Any) -> dict[str, Any]:
        raw = _request_mapping(payload)
        try:
            target = self._target(raw)
            root, _ = self._authorized_root(target, context)
            limits = _budget(raw)
            if bool(getattr(root, "follow_symlinks", False)):
                raise SafeServiceError("symlink_traversal_disabled")
            adapter = make_adapter(replace(root, follow_symlinks=False))
            accepted, skipped, _ = _bounded_candidates(adapter, target, limits)
            refs = []
            total_size = 0
            for candidate, size in accepted:
                refs.append(_reference(candidate, workspace=self.workspace, root_id=_text(root.root_id)))
                total_size += size
            return _ready(
                self.service_name,
                root_id=_safe_label(root.root_id, "<redacted>"),
                reference=_reference(target, workspace=self.workspace, root_id=_text(root.root_id)),
                candidate_count=len(refs),
                total_size=total_size,
                truncated_count=skipped,
                references=refs,
            )
        except SafeServiceError as exc:
            return _error(exc.code, service=self.service_name)
        except (OSError, ValueError, TypeError):
            return _error("source_inventory_failed", service=self.service_name)

    def preview(self, payload: Any = None, *, context: Any = None, **_: Any) -> dict[str, Any]:
        raw = _request_mapping(payload)
        detected = self.detect(raw, context=context)
        if detected.get("status") != "READY":
            return detected
        try:
            target = self._target(raw)
            root, _ = self._authorized_root(target, context)
            limits = _budget(raw)
            if bool(getattr(root, "follow_symlinks", False)):
                raise SafeServiceError("symlink_traversal_disabled")
            adapter = make_adapter(replace(root, follow_symlinks=False))
            accepted, skipped, deadline = _bounded_candidates(adapter, target, limits)
            items: list[dict[str, Any]] = []
            total_size = 0
            for candidate, size in accepted:
                if time.monotonic() >= deadline:
                    raise SafeServiceError("scan_timeout")
                try:
                    digest = _sha256_path(candidate, deadline)
                except (OSError, ValueError):
                    skipped += 1
                    continue
                total_size += size
                ext = candidate.suffix.casefold()
                supported = ext in TEXT_EXTS or ext in META_EXTS or candidate.name in INSTRUCTION_FILES
                items.append({
                    "reference": _reference(candidate, workspace=self.workspace, root_id=_text(root.root_id)),
                    "relative_reference": _reference(candidate, workspace=self.workspace, root_id=_text(root.root_id)),
                    "size": size,
                    "media_type": TEXT_MEDIA_TYPES.get(ext, "text/plain" if supported else "application/octet-stream"),
                    "hash": f"sha256:{digest}",
                    "supported": supported,
                })
            summary = {
                "candidate_count": len(items),
                "total_size": total_size,
                "skipped_count": skipped,
                "supported_count": sum(1 for item in items if item["supported"]),
            }
            return _ready(
                self.service_name,
                root_id=_safe_label(root.root_id, "<redacted>"),
                reference=_reference(target, workspace=self.workspace, root_id=_text(root.root_id)),
                summary=summary,
                items=items,
            )
        except SafeServiceError as exc:
            return _error(exc.code, service=self.service_name)
        except (OSError, ValueError, TypeError):
            return _error("import_preview_failed", service=self.service_name)

    # Public MCP semantic aliases.
    memoryguard_import_preview = preview

    def dispatch(self, name: str, payload: Any = None, *, context: Any = None, **kwargs: Any) -> dict[str, Any]:
        handlers = {
            "detect": self.detect,
            "inventory": self.inventory,
            "preview": self.preview,
            "memoryguard_import_preview": self.preview,
        }
        handler = handlers.get(_text(name))
        return handler(payload, context=context, **kwargs) if handler else _error("unknown_import_operation", service=self.service_name)

    call = dispatch


class RuntimeDiagnosticsService:
    """Stable runtime diagnostics with an explicit admin detail gate."""

    service_name = "runtime_processes"

    def __init__(
        self,
        workspace: str | Path,
        *,
        version_provider: Any = None,
        status_provider: Any = None,
    ) -> None:
        self.workspace = _canonical(Path(workspace))
        self.version_provider = version_provider
        self.status_provider = status_provider

    def _version(self) -> str:
        provider = self.version_provider
        if provider is None:
            try:
                from ..runtime_lease import memoryguard_version
                provider = memoryguard_version
            except Exception:  # pragma: no cover - package import failure
                provider = lambda: "unknown"
        try:
            return _safe_marker(_call_provider(provider, self.workspace), "unknown")
        except Exception:  # noqa: BLE001 - diagnostics never fail open
            return "unknown"

    def _status(self) -> Mapping[str, Any]:
        provider = self.status_provider
        if provider is None:
            try:
                from ..runtime_lease import runtime_lease_status
                provider = runtime_lease_status
            except Exception:  # pragma: no cover - package import failure
                return {}
        try:
            value = _call_provider(provider, self.workspace)
        except Exception:  # noqa: BLE001 - diagnostics are best effort
            return {}
        return value if isinstance(value, Mapping) else {"state": _text(value)}

    def memoryguard_runtime_processes(self, payload: Any = None, *, context: Any = None, **_: Any) -> dict[str, Any]:
        del payload
        details = self._status()
        live = details.get("live")
        stale = details.get("stale")
        conflicts = details.get("conflicting")
        live_count = len(live) if isinstance(live, (list, tuple)) else _count(details.get("live_count", 0))
        stale_count = len(stale) if isinstance(stale, (list, tuple)) else _count(details.get("stale_count", 0))
        conflict_count = len(conflicts) if isinstance(conflicts, (list, tuple)) else _count(details.get("conflict_count", 0))
        runtime_state = _safe_marker(
            details.get("state") or details.get("status") or "V2_ACTIVE",
            "UNKNOWN",
        )
        summary = {
            "live_processes": _count(live_count),
            "stale_leases": _count(stale_count),
            "conflicts": _count(conflict_count),
            "split_brain": bool(details.get("split_brain", False)),
            "restart_required": bool(details.get("restart_required", False)),
        }
        result = _ready(
            self.service_name,
            memoryguard_version=self._version(),
            runtime_status=runtime_state,
            summary=summary,
        )
        if _is_admin(context):
            # Details use a fixed allow-list and the sanitizer as a second
            # fence.  No path, command line, token or secret is ever exposed.
            allow = {"pid", "memoryguard_version", "code_fingerprint", "split_brain", "restart_required"}
            raw_details = {
                key: details[key]
                for key in allow
                if key in details
            }
            if isinstance(live, (list, tuple)):
                raw_details["live"] = live
            if isinstance(stale, (list, tuple)):
                raw_details["stale"] = stale
            if isinstance(conflicts, (list, tuple)):
                raw_details["conflicting"] = conflicts
            result["details"] = _safe_json(raw_details, include_details=True)
        return result

    runtime_processes = memoryguard_runtime_processes

    def dispatch(self, name: str = "memoryguard_runtime_processes", payload: Any = None, *, context: Any = None, **kwargs: Any) -> dict[str, Any]:
        if _text(name) not in {"runtime_processes", "memoryguard_runtime_processes"}:
            return _error("unknown_runtime_operation", service=self.service_name)
        return self.memoryguard_runtime_processes(payload, context=context, **kwargs)

    call = dispatch


__all__ = [
    "ImportPreviewService",
    "PureSourceReadService",
    "RuntimeDiagnosticsService",
    "SafeServiceError",
]
