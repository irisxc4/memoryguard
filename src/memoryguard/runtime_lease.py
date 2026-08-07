"""Multi-MCP runtime lease + split-brain detection (Req10).

Several MCP processes may legitimately share one control workspace (multi-MCP
is a feature).  A *runtime lease* records which live process holds the
workspace and its database files.  When a live process already holds the same
database set but was built from a different ``memoryguard_version`` or
``code_fingerprint``, that is a runtime split-brain: the newly arriving
process **must fail closed** (never write) and the operator is told to restart
it.  We never kill other processes — this module only probes liveness and
refuses to grant a write lease.

Lease file layout (JSON array, one entry per live process)::

    workspace/.memoryguard/runtime_leases.json
      [ {"pid": 1234, "process_started_at": "...", "memoryguard_version": "0.5.2",
         "code_fingerprint": "<sha256>", "control_workspace": "C:/...",
         "database_paths": ["C:/.../rule-intelligence/memory.db", ...]}, ... ]

``check_runtime_lease`` is the single entry point: no conflicting live lease →
it upserts this process's lease and returns ``granted=True``; a conflicting
live lease (same DB set, different version *or* fingerprint) → returns
``granted=False, split_brain=True, restart_required=True`` and lists the
conflicting entries without touching those processes.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .governance_lock import WorkspaceGovernanceLock

LEASE_FILE_NAME = "runtime_leases.json"
_LEASE_VERSION = 1
# ``process_started_at`` is captured once when the module is first imported
# (≈ process start, before any request is handled).
_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# identity helpers
# ---------------------------------------------------------------------------


def process_started_at() -> str:
    """ISO-8601 start time of the current process (module-import proxy)."""
    return _PROCESS_STARTED_AT


def memoryguard_version() -> str:
    """Best-effort installed version: importlib.metadata → package __version__.

    ``importlib.metadata.version("memoryguard")`` is the required source; the
    distribution is normally named ``agent-memguard`` while the import package
    is ``memoryguard``, so the package ``__version__`` is the reliable
    fallback and ``"unknown"`` is the final resort.
    """
    for dist in ("memoryguard", "agent-memguard"):
        try:
            import importlib.metadata
            return importlib.metadata.version(dist)
        except Exception:  # noqa: BLE001 - metadata lookup must never raise here
            continue
    try:
        from . import __version__
        return str(__version__ or "")
    except Exception:  # noqa: BLE001
        return "unknown"


_FINGERPRINT_CACHE: dict[tuple[Any, ...], str] = {}


def compute_code_fingerprint(package_dir: str | Path | None = None) -> str:
    """Stable sha256 over the package source ``.py`` files (read-only).

    The full package tree is hashed once per change; a stat signature
    (path + size + mtime) is cached so repeated calls only re-hash when a
    source file actually changed.  Nothing is written anywhere.
    """
    package_dir = Path(package_dir or Path(__file__).resolve().parent).resolve()
    try:
        files = sorted(
            p for p in package_dir.rglob("*.py")
            if p.is_file() and "__pycache__" not in p.parts
        )
    except OSError:
        files = []
    signature: list[tuple[Any, ...]] = []
    for p in files:
        try:
            st = p.stat()
        except OSError:
            continue
        signature.append((str(p.relative_to(package_dir)), st.st_size, st.st_mtime_ns))
    sig_tuple = tuple(signature)
    cached = _FINGERPRINT_CACHE.get((package_dir, sig_tuple))
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    for p in files:
        try:
            rel = str(p.relative_to(package_dir)).replace("\\", "/")
            data = p.read_bytes()
        except OSError:
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(data)
    fingerprint = digest.hexdigest()
    _FINGERPRINT_CACHE[(package_dir, sig_tuple)] = fingerprint
    return fingerprint


def default_database_paths(control_workspace: str | Path) -> list[str]:
    """DB files a process controls for one workspace: the rule-intelligence
    DB plus the shared-memory group DBs (the default group and any existing
    groups).  Deterministic and stable across processes on the same
    workspace, which is what lease identity needs."""
    workspace = Path(control_workspace).resolve()
    paths = {
        str((workspace / ".memoryguard" / "rule-intelligence" / "memory.db").resolve()),
        str((workspace / ".memoryguard" / "shared-memory" / "default" / "memory.db").resolve()),
    }
    base = workspace / ".memoryguard" / "shared-memory"
    if base.exists():
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            db = child / "memory.db"
            if db.exists():
                paths.add(str(db.resolve()))
    return sorted(paths)


# ---------------------------------------------------------------------------
# liveness + path helpers
# ---------------------------------------------------------------------------


def _win_pid_alive(pid: int) -> bool:
    """Read-only Windows process existence probe.

    Python's ``os.kill(pid, 0)`` is *not* a signal-0 probe on Windows; any
    non-ctrl signal calls ``TerminateProcess`` and would kill the peer we are
    checking.  OpenProcess + GetExitCodeProcess only reads process state.
    Access-denied is treated as alive so elevated peers are not mistaken for
    stale leases.
    """
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
    )
    if not handle:
        error = ctypes.get_last_error()
        # ERROR_INVALID_PARAMETER means no such pid; access denied is unknown.
        return error not in (0, 87)
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True  # fail closed: do not prune a peer we cannot inspect
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: Any) -> bool:
    """True when ``pid`` names a live process.  Never kills on any platform."""
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    if os.name == "nt":
        return _win_pid_alive(pid_int)
    try:
        os.kill(pid_int, 0)
    except PermissionError:
        return True  # exists, but this process may not signal it
    except Exception:  # noqa: BLE001 - ProcessLookupError etc. → dead
        return False
    return True


def _norm_path(path: Any) -> str:
    if not path:
        return ""
    normalized = os.path.realpath(
        os.path.abspath(os.path.expanduser(str(path))),
    )
    return os.path.normcase(os.path.normpath(normalized))


def _lease_identity(lease: dict[str, Any]) -> tuple[Any, ...]:
    return (str(lease.get("pid", "")), _norm_path(lease.get("control_workspace", "")))


def _db_conflict(
    lease: dict[str, Any],
    control_workspace: str | Path,
    our_paths: set[str],
) -> bool:
    """A lease holds the same DB set when it controls the same workspace or
    its recorded database files overlap ours."""
    if _norm_path(lease.get("control_workspace", "")) and _norm_path(
        lease.get("control_workspace", "")
    ) == _norm_path(str(control_workspace)):
        return True
    lease_paths = {_norm_path(p) for p in (lease.get("database_paths") or [])}
    return bool(our_paths and lease_paths and (our_paths & lease_paths))


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


class RuntimeLeaseStore:
    """JSON-persisted runtime leases for one control workspace.

    Mutations run under the workspace governance lock so two MCP processes
    acquiring concurrently never lose each other's lease (read-modify-write is
    atomic across processes).
    """

    def __init__(
        self,
        control_workspace: str | Path,
        *,
        leases_path: str | Path | None = None,
    ) -> None:
        self.control_workspace = Path(control_workspace).resolve()
        self.leases_path = (
            Path(leases_path).resolve()
            if leases_path is not None
            else self.control_workspace / ".memoryguard" / LEASE_FILE_NAME
        )

    # ------------------------------------------------------------- io

    def load(self) -> list[dict[str, Any]]:
        """Read the lease array; a missing/corrupt file yields ``[]``.

        Accepts both the historical plain-array layout and the wrapped
        ``{"version": N, "leases": [...]}`` layout this store writes.
        """
        if not self.leases_path.exists():
            return []
        try:
            data = json.loads(self.leases_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if isinstance(data, dict):
            data = data.get("leases", [])
        if not isinstance(data, list):
            return []
        return [entry for entry in data if isinstance(entry, dict)]

    def save(self, leases: list[dict[str, Any]]) -> None:
        """Atomic write: temp file + ``os.replace``, so a reader never sees a
        half-written lease file."""
        self.leases_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.leases_path.with_suffix(self.leases_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {"version": _LEASE_VERSION, "leases": leases},
                ensure_ascii=False, indent=2, sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.leases_path)

    def upsert(self, lease: dict[str, Any]) -> None:
        leases = self.load()
        identity = _lease_identity(lease)
        leases = [l for l in leases if _lease_identity(l) != identity]
        leases.append(lease)
        self.save(leases)

    def remove(self, pid: Any, *, control_workspace: str | Path | None = None) -> int:
        leases = self.load()
        ws = _norm_path(control_workspace) if control_workspace is not None else ""
        kept: list[dict[str, Any]] = []
        removed = 0
        for lease in leases:
            if str(lease.get("pid", "")) == str(pid) and (
                not ws or _norm_path(lease.get("control_workspace", "")) == ws
            ):
                removed += 1
                continue
            kept.append(lease)
        if removed:
            self.save(kept)
        return removed

    def prune_stale(self) -> int:
        """Drop leases whose pid is no longer alive; persist only on change."""
        leases = self.load()
        live = [l for l in leases if _pid_alive(l.get("pid"))]
        pruned = len(leases) - len(live)
        if pruned:
            self.save(live)
        return pruned

    def list_live(self) -> list[dict[str, Any]]:
        return [l for l in self.load() if _pid_alive(l.get("pid"))]


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def check_runtime_lease(
    control_workspace: str | Path,
    database_paths: list[str] | None = None,
    *,
    version: str | None = None,
    code_fingerprint: str | None = None,
    pid: int | None = None,
    process_started_at: str | None = None,
    leases_path: str | Path | None = None,
) -> dict[str, Any]:
    """Check the workspace's runtime leases and (if safe) take one for the
    current process.

    Returns::

        {"granted": bool, "split_brain": bool, "restart_required": bool,
         "conflicting": [lease, ...], "pruned": int, "leases_path": str}

    * no conflicting live lease            → upsert this process's lease,
                                             ``granted=True``
    * live lease, same version+fingerprint → reuse, ``granted=True``
    * live lease, different version OR fingerprint on the same DB set
        → ``granted=False, split_brain=True, restart_required=True``,
        conflicting lists the offending leases.  No process is ever killed.
    """
    workspace = Path(control_workspace).resolve()
    store = RuntimeLeaseStore(workspace, leases_path=leases_path)
    our_paths = {_norm_path(p) for p in (database_paths or default_database_paths(workspace))}
    ver = str(version or "") or memoryguard_version()
    fp = str(code_fingerprint or "") if code_fingerprint else compute_code_fingerprint()
    my_pid = int(pid or os.getpid())
    started = process_started_at or _PROCESS_STARTED_AT

    with WorkspaceGovernanceLock(workspace):
        pruned = store.prune_stale()
        other = [
            lease for lease in store.load()
            if str(lease.get("pid", "")) != str(my_pid)
        ]
        conflicts = [
            lease for lease in other
            if _db_conflict(lease, workspace, our_paths)
            and (
                str(lease.get("memoryguard_version", "") or "") != ver
                or str(lease.get("code_fingerprint", "") or "") != fp
            )
        ]
        if conflicts:
            return {
                "granted": False,
                "split_brain": True,
                "restart_required": True,
                "conflicting": conflicts,
                "pruned": pruned,
                "leases_path": str(store.leases_path),
            }
        store.upsert({
            "pid": my_pid,
            "process_started_at": started,
            "memoryguard_version": ver,
            "code_fingerprint": fp,
            "control_workspace": str(workspace),
            "database_paths": sorted(our_paths),
        })
    return {
        "granted": True,
        "split_brain": False,
        "restart_required": False,
        "conflicting": [],
        "pruned": pruned,
        "leases_path": str(store.leases_path),
    }


def release_runtime_lease(
    control_workspace: str | Path,
    pid: int | None = None,
    *,
    leases_path: str | Path | None = None,
) -> bool:
    """Remove this process's lease entry.  Idempotent: ``False`` when nothing
    was removed (already released / never held)."""
    store = RuntimeLeaseStore(control_workspace, leases_path=leases_path)
    with WorkspaceGovernanceLock(store.control_workspace):
        return store.remove(pid or os.getpid(), control_workspace=store.control_workspace) > 0


def runtime_lease_status(
    control_workspace: str | Path,
    *,
    version: str | None = None,
    code_fingerprint: str | None = None,
    leases_path: str | Path | None = None,
) -> dict[str, Any]:
    """Read-only split-brain status for diagnostics.  Never writes a lease;
    stale entries are reported but not pruned (``stale`` count)."""
    workspace = Path(control_workspace).resolve()
    store = RuntimeLeaseStore(workspace, leases_path=leases_path)
    leases = store.load()
    stale = [l for l in leases if not _pid_alive(l.get("pid"))]
    live = [l for l in leases if _pid_alive(l.get("pid"))]
    ver = str(version or "") or memoryguard_version()
    fp = str(code_fingerprint or "") if code_fingerprint else compute_code_fingerprint()
    our_paths = {_norm_path(p) for p in default_database_paths(workspace)}
    conflicts = [
        l for l in live
        if _db_conflict(l, workspace, our_paths)
        and (
            str(l.get("memoryguard_version", "") or "") != ver
            or str(l.get("code_fingerprint", "") or "") != fp
        )
    ]
    return {
        "split_brain": bool(conflicts),
        "restart_required": bool(conflicts),
        "conflicting": conflicts,
        "stale": stale,
        "live": live,
        "leases_path": str(store.leases_path),
    }


if __name__ == "__main__":  # pragma: no cover - manual smoke
    result = check_runtime_lease(sys.argv[1] if len(sys.argv) > 1 else ".")
    print(json.dumps(result, ensure_ascii=False, indent=2))
