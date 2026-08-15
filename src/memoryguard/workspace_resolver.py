"""Deterministic, bounded resolution of the active MemoryGuard workspace.

Bare entrypoints may be launched from a project container that also has an
older ``.memoryguard`` directory.  This module is the one read-only discovery
policy shared by CLI, GUI, and MCP transport.  It deliberately inspects only
the current directory, its bounded ancestor chain, and the current directory's
direct children; it never recursively scans a user or workspace root.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Callable, Iterable

from .data_home import is_v2_data_home


_MAX_ANCESTOR_DEPTH = 6
_V2_RUNTIME_STATES = frozenset({"V2_ACTIVE", "V2_READY"})
_STATE_PRIORITY = {
    "V2_ACTIVE": 0,
    "V2_READY": 1,
    "V2_BUILDING": 2,
    "UNKNOWN": 3,
}

_MIGRATION_SOURCE_ARTIFACTS = frozenset(
    {
        "agent-bindings",
        "agent_bindings",
        "config.json",
        "config.local.json",
        "governance.lock",
        "managed-memory",
        "managed_memory",
        "manifest.json",
        "shared-memory",
        "shared_memory",
    }
)


@dataclass(frozen=True)
class WorkspaceCandidate:
    """One bounded candidate and the state observed without creating state."""

    path: Path
    state: str
    source: str
    distance: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": str(self.path),
            "state": self.state,
            "source": self.source,
        }


class WorkspaceResolutionError(ValueError):
    """A bare launch cannot select one authoritative workspace safely."""

    code = "workspace_resolution_ambiguous"

    def __init__(self, candidates: Iterable[WorkspaceCandidate]) -> None:
        self.candidates = tuple(candidates)
        detail = "; ".join(
            f"{item.path} [{item.state}]" for item in self.candidates
        )
        super().__init__(
            "ambiguous active MemoryGuard workspaces; pass --workspace explicitly"
            + (f": {detail}" if detail else "")
        )

    def to_payload(self, *, surface: str) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "blocked",
            "code": self.code,
            "error": self.code,
            "surface": surface,
            "message": str(self),
            "candidates": [item.to_dict() for item in self.candidates],
        }


ManifestReader = Callable[[Path], Any]


def _default_manifest_reader(path: Path) -> tuple[str, int | None, Any]:
    """Read a candidate manifest without creating a workspace or database."""

    # Runtime discovery is V2-only.  A non-V2 project tree is a migration
    # source candidate, never a manifest reader input.
    if not is_v2_data_home(path):
        return "UNKNOWN", None, None

    from .system.manifest import ManifestManager

    try:
        record = ManifestManager(path).current()
    except Exception:
        return "UNKNOWN", None, None
    state = getattr(record, "state", "")
    generation = getattr(record, "generation", None)
    if isinstance(record, dict):
        state = record.get("state", record.get("status", ""))
        generation = record.get("generation")
    state = str(getattr(state, "value", state) or "").strip().upper()
    if state not in _STATE_PRIORITY:
        state = "UNKNOWN"
    return state, generation, record


def _read_state(path: Path, reader: ManifestReader) -> str:
    try:
        value = reader(path)
    except Exception:
        return "UNKNOWN"
    if isinstance(value, tuple) and value:
        value = value[0]
    elif isinstance(value, dict):
        value = value.get("state", value.get("status", ""))
    else:
        value = getattr(value, "state", value)
    state = str(getattr(value, "value", value) or "").strip().upper()
    return state if state in _STATE_PRIORITY else "UNKNOWN"


def _normalise(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _iter_bounded_candidates(cwd: Path) -> Iterable[tuple[Path, str, int]]:
    """Yield only the bounded local candidate set, in deterministic order."""

    yield cwd, "cwd", 0

    try:
        children = sorted(
            (
                item
                for item in cwd.iterdir()
                if item.is_dir() and not item.is_symlink()
            ),
            key=lambda item: str(item).casefold(),
        )
    except (OSError, PermissionError):
        children = []
    for child in children:
        yield child.resolve(), "cwd-child", 1

    ancestor = cwd.parent
    for distance in range(1, _MAX_ANCESTOR_DEPTH + 1):
        if ancestor == cwd or ancestor.parent == ancestor:
            break
        yield ancestor, "ancestor", distance
        ancestor = ancestor.parent


def _discover(cwd: Path, reader: ManifestReader) -> Path:
    candidates: list[WorkspaceCandidate] = []
    seen: set[Path] = set()
    for raw_path, source, distance in _iter_bounded_candidates(cwd):
        path = _normalise(raw_path)
        if (
            path in seen
            or not path.is_dir()
            or not (path / ".memoryguard").is_dir()
            or not is_v2_data_home(path)
        ):
            continue
        seen.add(path)
        candidates.append(
            WorkspaceCandidate(
                path=path,
                state=_read_state(path, reader),
                source=source,
                distance=distance,
            )
        )

    runtime = [item for item in candidates if item.state in _V2_RUNTIME_STATES]
    if runtime:
        best_priority = min(_STATE_PRIORITY[item.state] for item in runtime)
        best = [item for item in runtime if _STATE_PRIORITY[item.state] == best_priority]
        if len(best) > 1:
            raise WorkspaceResolutionError(
                sorted(best, key=lambda item: str(item.path).casefold())
            )
        return best[0].path

    # With no usable V2 manifest, retain the ordinary cwd semantics.  This is
    # important for upgrade/doctor diagnostics and never selects a child V1
    # workspace merely because it happens to exist.
    if cwd in seen:
        return cwd
    return cwd


def _looks_like_migration_source(path: Path) -> bool:
    """Recognise only exact project-local artifacts eligible for upgrade."""
    if is_v2_data_home(path):
        return False
    control_root = path / ".memoryguard"
    if not control_root.is_dir():
        return False
    return any((control_root / name).exists() for name in _MIGRATION_SOURCE_ARTIFACTS)


def discover_migration_source(
    cwd: str | Path | None = None,
    *,
    data_home: str | Path | None = None,
) -> Path | None:
    """Find one bounded, trusted project source for a bare upgrade.

    This is deliberately a migration seam.  It performs marker-only
    inspection and never opens a legacy manifest, database, or binding.
    Runtime callers must use :func:`resolve_data_home` for their control
    plane instead.
    """
    base = _normalise(cwd or Path.cwd())
    excluded = _normalise(data_home) if data_home is not None else None
    for raw_path, source, _distance in _iter_bounded_candidates(base):
        # A bare upgrade may inspect the current project and its bounded
        # ancestors.  Child projects are not trusted as an implicit source.
        if source == "cwd-child":
            continue
        path = _normalise(raw_path)
        if excluded is not None and path == excluded:
            continue
        if _looks_like_migration_source(path):
            return path
    return None


def resolve_workspace(
    requested: str | Path | None = None,
    *,
    cwd: str | Path | None = None,
    explicit: bool = False,
    manifest_reader: ManifestReader | None = None,
) -> Path:
    """Resolve one workspace using explicit, configured, then bounded bare mode.

    ``explicit`` is controlled by the caller because a CLI parser cannot infer
    whether a default ``.`` was supplied by a user or by argparse.  In bare
    mode an explicitly configured ``MEMORYGUARD_WORKSPACE`` remains authoritative.
    """

    if explicit:
        if requested is None:
            raise ValueError("explicit workspace path is missing")
        return _normalise(requested)

    configured = os.environ.get("MEMORYGUARD_WORKSPACE", "").strip()
    if configured:
        return _normalise(configured)

    base = _normalise(cwd or Path.cwd())
    return _discover(base, manifest_reader or _default_manifest_reader)


__all__ = [
    "WorkspaceCandidate",
    "WorkspaceResolutionError",
    "discover_migration_source",
    "resolve_workspace",
]
