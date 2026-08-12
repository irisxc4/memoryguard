"""Small public-safety helpers shared by V2 runtime entrypoints.

This module is intentionally dependency-light.  MCP, Hook, and Provider
entrypoints use it directly so public error sanitisation cannot pull the
compatibility adapter (or any legacy runtime) into the production seam.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any


_SAFE_CODE_RE = re.compile(
    r"^[a-z][a-z0-9_.-]*(?::[a-z0-9_.-]+(?:,[a-z0-9_.-]+)*)?$"
)
_ERROR_KEYS = frozenset({"error", "detail", "exception", "traceback", "sql", "query"})
_PATH_KEYS = frozenset({"workspace", "source_path", "absolute_path", "canonical_store_path"})

V2_UPGRADE_CODE = "v2_upgrade_required"
V2_UPGRADE_COMMAND = "memoryguard upgrade"


def safe_error_code(value: Any, fallback: str = "operation_failed") -> str:
    """Return stable public code without reflecting arbitrary error text."""
    candidate = str(value or "").strip().casefold()
    if len(candidate) <= 128 and _SAFE_CODE_RE.fullmatch(candidate):
        return candidate
    return str(fallback or "operation_failed")


def safe_exception_diagnostic(exc: BaseException, *, code: str) -> dict[str, str]:
    """Expose exception type and non-reversible hash, never exception text."""
    typename = type(exc).__name__ or "Exception"
    digest = hashlib.sha256(
        f"{typename}\x00{str(exc)}".encode("utf-8", "replace"),
    ).hexdigest()[:16]
    return {"type": typename, "hash": digest, "code": safe_error_code(code)}


def sanitize_public_payload(value: Any, *, error_code: str = "operation_failed") -> Any:
    """Redact error/path fields in a public mapping while preserving data."""
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            lowered = key.casefold()
            if lowered in _ERROR_KEYS:
                if lowered == "error":
                    output[key] = safe_error_code(raw_value, error_code)
                continue
            if lowered == "code":
                output[key] = safe_error_code(raw_value, error_code)
                continue
            if lowered in _PATH_KEYS:
                output[key] = "<redacted>"
                continue
            if lowered == "path" and isinstance(raw_value, str):
                if raw_value.startswith(("/", "\\")) or (
                    len(raw_value) > 2 and raw_value[1] == ":"
                ):
                    output[key] = "<redacted>"
                    continue
            output[key] = sanitize_public_payload(raw_value, error_code=error_code)
        return output
    if isinstance(value, (list, tuple)):
        return [sanitize_public_payload(item, error_code=error_code) for item in value]
    return value


def v2_upgrade_payload(state: str, *, surface: str) -> dict[str, Any]:
    """Build stable, non-sensitive retirement guidance for a V2 entrypoint."""
    normalized = str(state or "UNKNOWN").strip().upper() or "UNKNOWN"
    error = (
        "v2_manifest_state_unavailable"
        if normalized == "UNKNOWN"
        else V2_UPGRADE_CODE
    )
    return {
        "ok": False,
        "error": error,
        "code": error,
        "state": normalized,
        "surface": str(surface or "runtime"),
        "next_step": f"Run `{V2_UPGRADE_COMMAND}` before retrying.",
    }


def v2_upgrade_message(state: str, *, surface: str) -> str:
    """Return concise human guidance without exposing workspace or exception."""
    payload = v2_upgrade_payload(state, surface=surface)
    return (
        f"MemoryGuard {surface} requires V2; state={payload['state']}. "
        f"{payload['next_step']}"
    )
