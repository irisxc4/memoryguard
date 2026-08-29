"""Best-effort compatibility shim for Codex Desktop stdio MCP lifecycle leaks.

Codex remains the lifecycle authority.  This module only reclaims Codex-owned
stdio MCP cohorts that survive after their thread released or replaced them.
Healthy native cleanup wins automatically, so a future Codex fix turns this
shim into a no-op instead of creating a competing lifecycle manager.

The shim is Windows-only, fail-open, and independently switchable with
``MEMORYGUARD_CODEX_MCP_LIFECYCLE=auto|off|force`` (default: ``auto``).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Protocol


ROOT_NAMES = frozenset({"node_repl.exe", "node.exe", "python.exe", "cmd.exe", "uvx.exe"})
ANCHOR_NAME = "node_repl.exe"
# Observed Codex stdio MCP spawn waves can lag the node_repl anchor by 3–6s.
COHORT_WINDOW_MS = 7_000
ASSIGN_WINDOW_MS = 20_000
NATIVE_CLEANUP_GRACE_MS = 5_000
POST_TOOL_PROBE_INTERVAL_MS = 5_000
LEGACY_ADOPTION_GRACE_MS = 5 * 60 * 1_000
LEASE_TTL_MS = 30 * 60 * 1_000
WRITER_LOCK_MATCH_MS = 2_500
WRITER_THREAD_ACTIVE_SKEW_MS = 2_000
WRITER_EVIDENCE_VERSION = 1
LIFECYCLE_ENV = "MEMORYGUARD_CODEX_MCP_LIFECYCLE"
LIFECYCLE_MODES = frozenset({"auto", "off", "force"})
LEASE_EVENTS = frozenset({"session_start", "user_prompt", "post_tool"})
SUPPORTED_EVENTS = LEASE_EVENTS | {"stop"}
_STATE_VERSION = 2
_STATE_INDEX_VERSION = 1
_STATE_SHARD_DIR = "codex-mcp-lifecycle"
_STATE_INDEX_FILE = "codex-mcp-lifecycle.json"
_STATE_RETENTION_MS = 7 * 24 * 60 * 60 * 1_000
_MAX_STATE_GENERATIONS = 16
SHIM_VERSION = "0.7.1.post18"

_ORPHAN_MCP_ROOTS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$codexPid = [int]$env:MEMORYGUARD_CODEX_PID
$all = @(Get-CimInstance Win32_Process)
$byId = @{}
foreach ($p in $all) { $byId[[int]$p.ProcessId] = $p }
$roots = @{}
foreach ($mcp in ($all | Where-Object { $_.Name -eq 'codebase-memory-mcp.exe' })) {
    $cursor = $mcp
    $guard = 0
    while ($null -ne $cursor -and $guard -lt 16) {
        $parentId = [int]$cursor.ParentProcessId
        if ($parentId -eq $codexPid) {
            $gp = Get-Process -Id ([int]$cursor.ProcessId) -ErrorAction SilentlyContinue
            if ($null -ne $gp) {
                $dto = [DateTimeOffset]$gp.StartTime
                $roots[[string]$cursor.ProcessId] = @{
                    pid = [int]$cursor.ProcessId
                    parent_pid = $parentId
                    name = [string]$cursor.Name
                    start_ms = [int64]$dto.ToUnixTimeMilliseconds()
                }
            }
            break
        }
        if ($parentId -le 0 -or -not $byId.ContainsKey($parentId)) { break }
        $cursor = $byId[$parentId]
        $guard += 1
    }
}
@($roots.Values) | ConvertTo-Json -Compress -Depth 4
"""


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    pid: int
    parent_pid: int
    name: str
    start_ms: int

    @property
    def normalized_name(self) -> str:
        return self.name.lower()


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    codex_pid: int
    direct_children: tuple[ProcessInfo, ...]
    codex_start_ms: int = 0


@dataclass(frozen=True, slots=True)
class ThreadLockEvidence:
    thread_id: str
    lock_mtime_ms: int
    created_at_ms: int
    updated_at_ms: int
    thread_source: str = ""

    @property
    def lease_id(self) -> str:
        digest = hashlib.sha256(self.thread_id.encode("utf-8", errors="replace")).hexdigest()[:24]
        return f"session:{digest}"


@dataclass(frozen=True, slots=True)
class Cohort:
    anchor_pid: int
    anchor_start_ms: int
    roots: tuple[ProcessInfo, ...]

    @property
    def key(self) -> str:
        return f"{self.anchor_pid}:{self.anchor_start_ms}"


class ProcessController(Protocol):
    def snapshot(self) -> ProcessSnapshot | None: ...

    def terminate_tree(self, process: ProcessInfo) -> bool: ...

    def orphan_mcp_roots(self, codex_pid: int) -> tuple[ProcessInfo, ...]: ...


class WindowsProcessController:
    """Discover the current Codex ancestor and its direct MCP-root children."""

    _SNAPSHOT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$cursor = Get-CimInstance Win32_Process -Filter ('ProcessId = {0}' -f $env:MEMORYGUARD_LIFECYCLE_PID)
$codexPid = 0
$guard = 0
while ($null -ne $cursor -and $guard -lt 12) {
    if ([string]::Equals([string]$cursor.Name, 'codex.exe', [System.StringComparison]::OrdinalIgnoreCase)) {
        $codexPid = [int]$cursor.ProcessId
        break
    }
    $parentId = [int]$cursor.ParentProcessId
    if ($parentId -le 0) { break }
    $cursor = Get-CimInstance Win32_Process -Filter ('ProcessId = {0}' -f $parentId)
    $guard += 1
}
if ($codexPid -le 0) {
    @{ codex_pid = 0; codex_start_ms = 0; children = @() } | ConvertTo-Json -Compress -Depth 4
    exit 0
}
$codexGp = Get-Process -Id $codexPid -ErrorAction SilentlyContinue
$codexStartMs = 0
if ($null -ne $codexGp) {
    $codexDto = [DateTimeOffset]$codexGp.StartTime
    $codexStartMs = [int64]$codexDto.ToUnixTimeMilliseconds()
}
$children = @()
Get-CimInstance Win32_Process -Filter ('ParentProcessId = {0}' -f $codexPid) | ForEach-Object {
    $gp = Get-Process -Id ([int]$_.ProcessId) -ErrorAction SilentlyContinue
    if ($null -ne $gp) {
        $dto = [DateTimeOffset]$gp.StartTime
        $children += @{
            pid = [int]$_.ProcessId
            parent_pid = [int]$_.ParentProcessId
            name = [string]$_.Name
            start_ms = [int64]$dto.ToUnixTimeMilliseconds()
        }
    }
}
@{ codex_pid = $codexPid; codex_start_ms = $codexStartMs; children = $children } | ConvertTo-Json -Compress -Depth 4
"""

    def __init__(
        self,
        *,
        timeout_seconds: float = 4.0,
        allow_termination: bool = False,
    ) -> None:
        self.timeout_seconds = max(0.5, float(timeout_seconds))
        self.allow_termination = bool(allow_termination)

    def snapshot(self) -> ProcessSnapshot | None:
        if os.name != "nt":
            return None
        env = os.environ.copy()
        env["MEMORYGUARD_LIFECYCLE_PID"] = str(os.getpid())
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                self._SNAPSHOT_SCRIPT,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=self.timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return None
        raw = json.loads(completed.stdout.strip())
        codex_pid = int(raw.get("codex_pid") or 0)
        codex_start_ms = int(raw.get("codex_start_ms") or 0)
        if codex_pid <= 0:
            return None
        children_raw = raw.get("children") or []
        if isinstance(children_raw, dict):
            children_raw = [children_raw]
        children: list[ProcessInfo] = []
        for item in children_raw:
            try:
                process = ProcessInfo(
                    pid=int(item["pid"]),
                    parent_pid=int(item["parent_pid"]),
                    name=str(item["name"]),
                    start_ms=int(item["start_ms"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if process.parent_pid == codex_pid:
                children.append(process)
        return ProcessSnapshot(
            codex_pid=codex_pid,
            direct_children=tuple(children),
            codex_start_ms=codex_start_ms,
        )

    def terminate_tree(self, process: ProcessInfo) -> bool:
        if (
            not self.allow_termination
            or os.name != "nt"
            or process.pid == os.getpid()
        ):
            return False
        completed = subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        return completed.returncode == 0

    def orphan_mcp_roots(self, codex_pid: int) -> tuple[ProcessInfo, ...]:
        if os.name != "nt" or codex_pid <= 0:
            return ()
        environment = os.environ.copy()
        environment["MEMORYGUARD_CODEX_PID"] = str(codex_pid)
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _ORPHAN_MCP_ROOTS_SCRIPT,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=self.timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return ()
        try:
            raw = json.loads(completed.stdout.strip())
        except (TypeError, ValueError):
            return ()
        if isinstance(raw, dict):
            raw = [raw]
        roots: list[ProcessInfo] = []
        for item in raw or []:
            try:
                process = ProcessInfo(
                    pid=int(item["pid"]),
                    parent_pid=int(item["parent_pid"]),
                    name=str(item["name"]),
                    start_ms=int(item["start_ms"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if process.parent_pid == codex_pid and process.normalized_name in ROOT_NAMES:
                roots.append(process)
        return tuple(roots)


def _mode(explicit: str | None = None) -> str:
    value = str(explicit if explicit is not None else os.environ.get(LIFECYCLE_ENV, "auto"))
    normalized = value.strip().lower()
    return normalized if normalized in LIFECYCLE_MODES else "auto"


def _runtime_state_dir(workspace: Path) -> Path:
    return workspace / ".memoryguard" / "hook-runtime"


def _state_index_path(workspace: Path) -> Path:
    """Stable diagnostics index; lifecycle mutations never share this state."""
    return _runtime_state_dir(workspace) / _STATE_INDEX_FILE


def _generation_key(snapshot: ProcessSnapshot) -> str:
    return f"{int(snapshot.codex_pid)}:{int(snapshot.codex_start_ms)}"


def _state_path(
    workspace: Path,
    snapshot: ProcessSnapshot | None = None,
) -> Path:
    """Return one state shard per concrete Codex process generation.

    Real Codex processes expose ``codex_start_ms``. Keeping PID and start time
    in the path prevents an ephemeral ``codex exec`` process, a second Desktop
    window, or PID reuse from overwriting another generation's live leases.
    Fake/legacy controllers without a start time retain the original path.
    """
    if (
        snapshot is not None
        and int(snapshot.codex_pid) > 0
        and int(snapshot.codex_start_ms) > 0
    ):
        return (
            _runtime_state_dir(workspace)
            / _STATE_SHARD_DIR
            / f"{int(snapshot.codex_pid)}-{int(snapshot.codex_start_ms)}.json"
        )
    return _state_index_path(workspace)


@contextmanager
def _state_lock(path: Path, timeout_seconds: float = 2.0) -> Iterator[None]:
    """Serialize tiny lease receipts without coupling to governance locks."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    fd: int | None = None
    try:
        while fd is None:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, f"{os.getpid()} {time.time_ns()}".encode("ascii"))
            except (FileExistsError, PermissionError):
                try:
                    if time.time() - lock_path.stat().st_mtime > 30.0:
                        lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("codex lifecycle state lock timeout")
                time.sleep(0.01)
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass


def _empty_state(
    codex_pid: int = 0,
    codex_start_ms: int = 0,
) -> dict[str, Any]:
    state = {
        "version": _STATE_VERSION,
        "codex_pid": int(codex_pid),
        "codex_start_ms": int(codex_start_ms),
        "threads": {},
        "retired": {},
    }
    if codex_pid > 0 and codex_start_ms > 0:
        state["generation_key"] = f"{int(codex_pid)}:{int(codex_start_ms)}"
        # A concrete Codex process generation begins with an empty lifecycle
        # baseline. If the first Hook observation contains exactly one cohort,
        # that cohort is a unique generation snapshot delta rather than a
        # nearest-time guess. Multiple restored cohorts remain ambiguous.
        state["observed_cohort_keys"] = []
    return state


def _load_state(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return _empty_state()
    if (
        not isinstance(raw, dict)
        or int(raw.get("version") or 0) != _STATE_VERSION
        or not isinstance(raw.get("threads"), dict)
        or not isinstance(raw.get("retired"), dict)
    ):
        return _empty_state()
    return raw


def _prepare_state(
    path: Path,
    snapshot: ProcessSnapshot,
    *,
    legacy_path: Path | None = None,
) -> dict[str, Any]:
    """Load one generation shard, optionally adopting a matching legacy state."""
    state: dict[str, Any] | None = None
    if path.is_file():
        state = _load_state(path)
    elif legacy_path is not None and legacy_path != path and legacy_path.is_file():
        legacy = _load_state(legacy_path)
        legacy_pid = int(legacy.get("codex_pid") or 0)
        legacy_start = int(legacy.get("codex_start_ms") or 0)
        if (
            legacy_pid == int(snapshot.codex_pid)
            and legacy_start in {0, int(snapshot.codex_start_ms)}
        ):
            state = legacy
            state["migrated_from_legacy_state"] = True

    if state is None:
        return _empty_state(snapshot.codex_pid, snapshot.codex_start_ms)
    if int(state.get("codex_pid") or 0) != int(snapshot.codex_pid):
        return _empty_state(snapshot.codex_pid, snapshot.codex_start_ms)
    state_start = int(state.get("codex_start_ms") or 0)
    if state_start not in {0, int(snapshot.codex_start_ms)}:
        return _empty_state(snapshot.codex_pid, snapshot.codex_start_ms)
    state["codex_pid"] = int(snapshot.codex_pid)
    state["codex_start_ms"] = int(snapshot.codex_start_ms)
    if snapshot.codex_start_ms > 0:
        state["generation_key"] = _generation_key(snapshot)
    return state


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _publish_state_index(
    workspace: Path,
    shard_path: Path,
    state: dict[str, Any],
) -> None:
    """Publish a bounded diagnostics index without sharing mutable lease state."""
    index_path = _state_index_path(workspace)
    if shard_path == index_path:
        return
    generation_key = str(state.get("generation_key") or "")
    if not generation_key:
        return
    updated_ms = int(state.get("updated_ms") or 0)
    threads = state.get("threads") if isinstance(state.get("threads"), dict) else {}
    retired = state.get("retired") if isinstance(state.get("retired"), dict) else {}
    entry = {
        "generation_key": generation_key,
        "codex_pid": int(state.get("codex_pid") or 0),
        "codex_start_ms": int(state.get("codex_start_ms") or 0),
        "state_path": str(shard_path),
        "shim_version": str(state.get("shim_version") or SHIM_VERSION),
        "updated_ms": updated_ms,
        "last_event": str(state.get("last_event") or ""),
        "last_thread_id": str(state.get("last_thread_id") or ""),
        "last_assignment_reason": str(state.get("last_assignment_reason") or ""),
        "last_cohort_count": int(state.get("last_cohort_count") or 0),
        "thread_count": len(threads),
        "owned_cohort_count": len(
            {
                str((lease or {}).get("cohort_key") or "")
                for lease in threads.values()
                if str((lease or {}).get("cohort_key") or "")
            }
        ),
        "retired_count": len(retired),
        "last_killed_pids": list(state.get("last_killed_pids") or []),
        "last_failed_pids": list(state.get("last_failed_pids") or []),
    }
    try:
        with _state_lock(index_path):
            current = _load_json_object(index_path)
            generations = current.get("generations")
            if not isinstance(generations, dict):
                generations = {}
            cutoff = int(time.time() * 1000) - _STATE_RETENTION_MS
            generations = {
                str(key): value
                for key, value in generations.items()
                if isinstance(value, dict)
                and int(value.get("updated_ms") or 0) >= cutoff
            }
            generations[generation_key] = entry
            ordered = sorted(
                generations.items(),
                key=lambda item: int((item[1] or {}).get("updated_ms") or 0),
                reverse=True,
            )[:_MAX_STATE_GENERATIONS]
            generations = dict(ordered)
            index = {
                "index_version": _STATE_INDEX_VERSION,
                "shim_version": SHIM_VERSION,
                "updated_ms": updated_ms,
                "latest_generation": generation_key,
                "latest_state_path": str(shard_path),
                "generations": generations,
            }
            _save_state(index_path, index)
    except (OSError, TimeoutError):
        return

    keep_paths = {
        str((value or {}).get("state_path") or "")
        for value in generations.values()
    }
    shard_dir = _runtime_state_dir(workspace) / _STATE_SHARD_DIR
    try:
        cutoff_seconds = (int(time.time() * 1000) - _STATE_RETENTION_MS) / 1000
        for candidate in shard_dir.glob("*.json"):
            if str(candidate) in keep_paths:
                continue
            if candidate.stat().st_mtime < cutoff_seconds:
                candidate.unlink(missing_ok=True)
    except OSError:
        pass


def _cohorts(snapshot: ProcessSnapshot, self_pid: int) -> tuple[Cohort, ...]:
    direct = [
        p
        for p in snapshot.direct_children
        if p.pid != self_pid
        and p.parent_pid == snapshot.codex_pid
        and p.normalized_name in ROOT_NAMES
    ]
    anchors = [p for p in direct if p.normalized_name == ANCHOR_NAME]
    others = [p for p in direct if p.normalized_name != ANCHOR_NAME]
    result: list[Cohort] = []
    used: set[int] = set()
    for anchor in sorted(anchors, key=lambda p: (p.start_ms, p.pid)):
        members = [anchor]
        used.add(anchor.pid)
        for process in sorted(others, key=lambda item: (item.start_ms, item.pid)):
            if process.pid in used:
                continue
            if abs(process.start_ms - anchor.start_ms) > COHORT_WINDOW_MS:
                continue
            nearest = min(
                anchors,
                key=lambda item: (abs(process.start_ms - item.start_ms), item.pid),
            )
            if nearest.pid != anchor.pid:
                continue
            members.append(process)
            used.add(process.pid)
        result.append(Cohort(anchor.pid, anchor.start_ms, tuple(members)))
    return tuple(result)


def _nearest(cohorts: tuple[Cohort, ...], now_ms: int) -> Cohort | None:
    if not cohorts:
        return None
    candidate = min(cohorts, key=lambda c: abs(c.anchor_start_ms - now_ms))
    return candidate if abs(candidate.anchor_start_ms - now_ms) <= ASSIGN_WINDOW_MS else None


def _codex_home() -> Path:
    configured = str(os.environ.get("CODEX_HOME", "") or "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _read_thread_lock_evidence(snapshot: ProcessSnapshot) -> tuple[ThreadLockEvidence, ...]:
    """Read host-owned writer-lock timing plus thread activity, without mutation."""
    if snapshot.codex_start_ms <= 0:
        return ()
    home = _codex_home()
    lock_root = home / "thread-writer-locks"
    db_path = home / "state_5.sqlite"
    try:
        lock_rows = {
            path.stem: int(path.stat().st_mtime * 1000)
            for path in lock_root.glob("*.lock")
            if not path.name.startswith(".")
            and int(path.stat().st_mtime * 1000)
            >= snapshot.codex_start_ms - WRITER_THREAD_ACTIVE_SKEW_MS
        }
    except OSError:
        return ()
    if not lock_rows or not db_path.is_file():
        return ()

    placeholders = ",".join("?" for _ in lock_rows)
    try:
        connection = sqlite3.connect(
            db_path.as_uri() + "?mode=ro",
            uri=True,
            timeout=0.25,
        )
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, created_at, updated_at, created_at_ms, updated_at_ms, "
            "thread_source FROM threads WHERE id IN (" + placeholders + ")",
            tuple(lock_rows),
        ).fetchall()
    except (OSError, sqlite3.Error, ValueError):
        return ()
    finally:
        try:
            connection.close()
        except (NameError, sqlite3.Error):
            pass

    evidence: list[ThreadLockEvidence] = []
    for row in rows:
        thread_id = str(row["id"] or "")
        if not thread_id or thread_id not in lock_rows:
            continue
        created_ms = int(row["created_at_ms"] or 0) or int(row["created_at"] or 0) * 1000
        updated_ms = int(row["updated_at_ms"] or 0) or int(row["updated_at"] or 0) * 1000
        evidence.append(
            ThreadLockEvidence(
                thread_id=thread_id,
                lock_mtime_ms=lock_rows[thread_id],
                created_at_ms=created_ms,
                updated_at_ms=updated_ms,
                thread_source=str(row["thread_source"] or ""),
            )
        )
    return tuple(evidence)


def _exact_writer_matches(
    cohorts: tuple[Cohort, ...],
    evidence: tuple[ThreadLockEvidence, ...],
) -> tuple[tuple[Cohort, ThreadLockEvidence], ...]:
    """Return only one-to-one lock/cohort matches; ambiguous timing stays untouched."""
    by_cohort: dict[str, list[ThreadLockEvidence]] = {}
    by_thread: dict[str, list[Cohort]] = {}
    for cohort in cohorts:
        matches = [
            item
            for item in evidence
            if abs(item.lock_mtime_ms - cohort.anchor_start_ms) <= WRITER_LOCK_MATCH_MS
        ]
        by_cohort[cohort.key] = matches
        for item in matches:
            by_thread.setdefault(item.thread_id, []).append(cohort)
    return tuple(
        (cohort, matches[0])
        for cohort in cohorts
        if len((matches := by_cohort.get(cohort.key, []))) == 1
        and len(by_thread.get(matches[0].thread_id, [])) == 1
    )


def _clear_cohort_owners(
    state: dict[str, Any],
    cohort_key: str,
    *,
    keep_thread: str = "",
) -> None:
    for thread_id, lease in (state.get("threads") or {}).items():
        if thread_id == keep_thread:
            continue
        if str((lease or {}).get("cohort_key") or "") == cohort_key:
            lease["cohort_key"] = ""
            if str(lease.get("turn_state") or "") == "active":
                lease["turn_state"] = "idle"


def _reconcile_writer_evidence(
    state: dict[str, Any],
    cohorts: tuple[Cohort, ...],
    snapshot: ProcessSnapshot,
    now_ms: int,
    *,
    reserved_cohort_key: str = "",
) -> dict[str, int]:
    """Correct guessed leases using exact writer-lock evidence before any GC."""
    evidence = _read_thread_lock_evidence(snapshot)
    matches = _exact_writer_matches(cohorts, evidence)
    by_key = {cohort.key: cohort for cohort in cohorts}
    threads = state.setdefault("threads", {})
    retired = state.setdefault("retired", {})
    stats = {
        "evidence_count": len(evidence),
        "exact_match_count": len(matches),
        "active_assigned_count": 0,
        "restored_preserved_count": 0,
        "snapshot_reserved_count": 0,
        "snapshot_owner_preserved_count": 0,
        # Kept for receipt compatibility with post2. Auto mode no longer
        # retires a resumable thread merely because this Codex generation has
        # not observed a fresh turn yet.
        "idle_retired_count": 0,
        "superseded_retired_count": 0,
    }

    for cohort, item in matches:
        if cohort.key == reserved_cohort_key:
            # A unique before/after process delta belongs to the Hook event
            # that observed it. A restored writer lock with a coincidental
            # timestamp must not steal that transport before it is leased.
            stats["snapshot_reserved_count"] += 1
            continue
        active_this_generation = (
            item.updated_at_ms
            >= snapshot.codex_start_ms - WRITER_THREAD_ACTIVE_SKEW_MS
        )
        expected_thread = item.lease_id
        snapshot_owners = [
            (owner_id, owner_lease)
            for owner_id, owner_lease in threads.items()
            if owner_id != expected_thread
            and str((owner_lease or {}).get("cohort_key") or "") == cohort.key
            and str((owner_lease or {}).get("assignment_reason") or "")
            == "snapshot_delta"
        ]
        if snapshot_owners:
            # Snapshot-delta is positive ownership evidence for this process
            # generation and survives Stop/resume. Writer-lock reconciliation
            # may refine guesses, but it may not reassign a proven live lease.
            stats["snapshot_owner_preserved_count"] += 1
            continue
        lease = threads.get(expected_thread)
        current_key = str((lease or {}).get("cohort_key") or "")
        current = by_key.get(current_key)
        _clear_cohort_owners(state, cohort.key, keep_thread=expected_thread)
        if (
            current is not None
            and current.key != cohort.key
            and current.anchor_start_ms > cohort.anchor_start_ms
        ):
            _retire(
                state,
                cohort.key,
                now_ms,
                "writer_lock_superseded",
                expected_thread,
            )
            stats["superseded_retired_count"] += 1
            continue

        evidence_kind = (
            "writer_lock" if active_this_generation else "writer_lock_restored"
        )
        if lease is None:
            lease = {
                "turn_started_ms": max(snapshot.codex_start_ms, item.lock_mtime_ms),
                "last_seen_ms": item.updated_at_ms,
                "cohort_key": cohort.key,
                "evidence": evidence_kind,
                "assignment_reason": evidence_kind,
                "pulse_count": 0,
            }
            threads[expected_thread] = lease
        else:
            lease["cohort_key"] = cohort.key
            lease["last_seen_ms"] = max(
                int(lease.get("last_seen_ms") or 0),
                item.updated_at_ms,
            )
            lease["evidence"] = evidence_kind
            lease.setdefault("assignment_reason", evidence_kind)
            lease.setdefault("pulse_count", 0)
        retired.pop(cohort.key, None)
        if active_this_generation:
            stats["active_assigned_count"] += 1
        else:
            stats["restored_preserved_count"] += 1

    state["writer_evidence_version"] = WRITER_EVIDENCE_VERSION
    state["writer_evidence"] = stats
    return stats


def _owned(state: dict[str, Any], excluding_thread: str | None = None) -> set[str]:
    return {
        str((lease or {}).get("cohort_key") or "")
        for thread_id, lease in (state.get("threads") or {}).items()
        if thread_id != excluding_thread and str((lease or {}).get("cohort_key") or "")
    }


def _observed_cohort_keys(state: dict[str, Any]) -> set[str] | None:
    """Return the prior live cohort snapshot, when this state has one.

    The field is intentionally a small set of stable ``pid:start_ms`` keys.
    Missing state is different from an empty snapshot: the first observation
    establishes the before-snapshot and must not be treated as a new cohort.
    """
    raw = state.get("observed_cohort_keys")
    if not isinstance(raw, list):
        return None
    return {str(value) for value in raw if str(value)}


def _unique_snapshot_delta(
    state: dict[str, Any],
    cohorts: tuple[Cohort, ...],
    thread_id: str,
) -> Cohort | None:
    """Bind only a unique cohort created after the prior hook snapshot.

    This is the safe late-start seam.  A nearest process is not evidence of
    ownership when several old cohorts exist, so an empty lease is adopted
    only when the persisted before/after snapshots contain exactly one new
    cohort and this event is the sole unresolved lease.  Any ambiguity stays
    fail-open.
    """
    previous = _observed_cohort_keys(state)
    if previous is None:
        return None
    owned_by_other = _owned(state, excluding_thread=thread_id)
    added = [
        cohort
        for cohort in cohorts
        if cohort.key not in previous and cohort.key not in owned_by_other
    ]
    if len(added) != 1:
        return None

    threads = state.setdefault("threads", {})
    other_unresolved = [
        candidate_thread
        for candidate_thread, lease in threads.items()
        if candidate_thread != thread_id
        and not str((lease or {}).get("cohort_key") or "")
    ]
    if other_unresolved:
        return None
    candidate = added[0]
    if candidate.key in _owned(state, excluding_thread=thread_id):
        return None
    return candidate


def _has_snapshot_delta(state: dict[str, Any], cohorts: tuple[Cohort, ...]) -> bool:
    prior = _observed_cohort_keys(state)
    return prior is not None and any(cohort.key not in prior for cohort in cohorts)


def _unique_unowned_cohort(
    cohorts: tuple[Cohort, ...],
    state: dict[str, Any],
) -> Cohort | None:
    """Return the sole live cohort not leased by any observed Hook session.

    This is an adoption-only fallback for a session first observed after its
    Codex runtime already exists.  It never retires or terminates a process.
    Ambiguous (>1) unowned cohorts are deliberately left untouched.
    """
    owned = _owned(state)
    unowned = [cohort for cohort in cohorts if cohort.key not in owned]
    return unowned[0] if len(unowned) == 1 else None


def _reconcile_unique_empty_lease(
    state: dict[str, Any],
    cohorts: tuple[Cohort, ...],
) -> None:
    """Pair the sole empty lease with the sole unowned cohort, if unambiguous."""
    threads = state.setdefault("threads", {})
    empty = [
        (thread_id, lease)
        for thread_id, lease in threads.items()
        if not str((lease or {}).get("cohort_key") or "")
    ]
    if len(empty) != 1:
        return
    candidate = _unique_unowned_cohort(cohorts, state)
    if candidate is None:
        return
    _, lease = empty[0]
    lease["cohort_key"] = candidate.key
    state.setdefault("retired", {}).pop(candidate.key, None)
    candidates = state.get("legacy_candidates")
    if isinstance(candidates, dict):
        candidates.pop(candidate.key, None)


def _reusable_retired(
    cohorts: tuple[Cohort, ...],
    state: dict[str, Any],
    thread_id: str,
    *,
    strict_thread: bool = False,
) -> Cohort | None:
    retired = state.setdefault("retired", {})
    candidates = [c for c in cohorts if c.key in retired]
    same_thread = [
        c for c in candidates if str((retired.get(c.key) or {}).get("thread_id") or "") == thread_id
    ]
    if len(same_thread) == 1:
        return same_thread[0]
    if strict_thread:
        return None
    return candidates[0] if len(candidates) == 1 else None


def _retire(state: dict[str, Any], key: str, now_ms: int, reason: str, thread_id: str) -> None:
    if not key:
        return
    receipt = state.setdefault("retired", {}).setdefault(key, {})
    receipt.setdefault("retired_ms", now_ms)
    receipt["last_seen_ms"] = now_ms
    receipt.setdefault("reason", reason)
    receipt.setdefault("thread_id", thread_id)


def _prune_leases(
    state: dict[str, Any],
    now_ms: int,
    live_cohort_keys: set[str],
) -> None:
    """Drop dead receipts only; inactivity is not a conversation-close signal.

    Codex ``Stop`` ends one turn, and a thread may be resumed much later with
    the same stdio transports. A live cohort therefore keeps its lease even
    after the bookkeeping TTL. Only empty leases, or leases whose process
    cohort has already disappeared natively, are removed.
    """
    threads = state.setdefault("threads", {})
    for thread_id in list(threads):
        lease = threads.get(thread_id) or {}
        if now_ms - int(lease.get("last_seen_ms") or 0) <= LEASE_TTL_MS:
            continue
        key = str(lease.get("cohort_key") or "")
        if key and key in live_cohort_keys:
            lease.setdefault("stale_since_ms", now_ms)
            continue
        threads.pop(thread_id, None)
        if key:
            state.setdefault("retired", {}).pop(key, None)


def _seed_legacy_adoption(
    state: dict[str, Any],
    cohorts: tuple[Cohort, ...],
    now_ms: int,
) -> None:
    """Quarantine pre-shim cohorts and reserve newest ones for observed sessions."""
    if state.get("legacy_started_ms") or state.get("legacy_candidates"):
        return
    threads = state.setdefault("threads", {})
    if not cohorts or not threads:
        return
    # This seam is reached only when no cohort is near the current Hook event,
    # so the live roots pre-date lifecycle observation.  Start the quarantine
    # clock at the first observed session activity rather than at this later
    # maintenance pulse.
    observed_starts = [
        int((lease or {}).get("turn_started_ms") or 0)
        for lease in threads.values()
        if int((lease or {}).get("turn_started_ms") or 0) > 0
    ]
    started_ms = min(observed_starts) if observed_starts else now_ms
    already_owned = _owned(state)
    candidates = {
        cohort.key: {"anchor_start_ms": cohort.anchor_start_ms}
        for cohort in cohorts
        if cohort.key not in already_owned
    }
    state["legacy_started_ms"] = started_ms
    state["legacy_candidates"] = candidates

    # Protect one newest live cohort for every session already observed by the
    # Hook.  Exact pre-shim ownership is unavailable, so this reservation is
    # deliberately conservative and does not terminate anything.
    available = sorted(
        (cohort for cohort in cohorts if cohort.key in candidates),
        key=lambda item: item.anchor_start_ms,
        reverse=True,
    )
    pending_threads = sorted(
        (
            (thread_id, lease)
            for thread_id, lease in threads.items()
            if not str((lease or {}).get("cohort_key") or "")
        ),
        key=lambda item: int((item[1] or {}).get("turn_started_ms") or now_ms),
    )
    for thread_id, lease in pending_threads:
        if not available:
            break
        cohort = available.pop(0)
        lease["cohort_key"] = cohort.key
        candidates.pop(cohort.key, None)


def _claim_legacy_candidate(
    state: dict[str, Any],
    cohorts: tuple[Cohort, ...],
) -> Cohort | None:
    candidates = state.get("legacy_candidates")
    if not isinstance(candidates, dict) or not candidates:
        return None
    by_key = {cohort.key: cohort for cohort in cohorts}
    available = [by_key[key] for key in candidates if key in by_key]
    if not available:
        return None
    cohort = max(available, key=lambda item: item.anchor_start_ms)
    candidates.pop(cohort.key, None)
    return cohort


def _retire_expired_legacy(
    state: dict[str, Any],
    by_key: dict[str, Cohort],
    owned: set[str],
    now_ms: int,
) -> None:
    candidates = state.get("legacy_candidates")
    started_ms = int(state.get("legacy_started_ms") or 0)
    if not isinstance(candidates, dict) or not candidates or started_ms <= 0:
        return
    if now_ms - started_ms < LEGACY_ADOPTION_GRACE_MS:
        return
    retired_at = started_ms + LEGACY_ADOPTION_GRACE_MS
    for key in list(candidates):
        if key in owned:
            candidates.pop(key, None)
            continue
        if key not in by_key:
            candidates.pop(key, None)
            continue
        receipt = state.setdefault("retired", {}).setdefault(key, {})
        receipt.setdefault("retired_ms", retired_at)
        receipt["last_seen_ms"] = now_ms
        receipt.setdefault("reason", "legacy_unclaimed")
        receipt.setdefault("thread_id", "")
        candidates.pop(key, None)


def _same_root(process: ProcessInfo, snapshot: ProcessSnapshot) -> bool:
    return any(
        current.pid == process.pid
        and current.parent_pid == snapshot.codex_pid
        and current.normalized_name == process.normalized_name
        and current.start_ms == process.start_ms
        for current in snapshot.direct_children
    )


def _terminate(
    controller: ProcessController, cohort: Cohort, self_pid: int
) -> tuple[list[int], list[int]]:
    fresh = controller.snapshot()
    if fresh is None:
        return [], [p.pid for p in cohort.roots]
    killed: list[int] = []
    failed: list[int] = []
    roots = sorted(cohort.roots, key=lambda p: (p.normalized_name == ANCHOR_NAME, p.pid))
    for process in roots:
        if process.pid == self_pid or not _same_root(process, fresh):
            continue
        (killed if controller.terminate_tree(process) else failed).append(process.pid)
    return killed, failed


def _cleanup_legacy_orphan_roots(
    controller: ProcessController,
    state: dict[str, Any],
    cohorts: tuple[Cohort, ...],
    now_ms: int,
    self_pid: int,
) -> tuple[list[int], list[int]]:
    """Drain only pre-activation orphan MCP root trees during legacy migration."""
    started_ms = int(state.get("legacy_started_ms") or 0)
    if (
        started_ms <= 0
        or now_ms - started_ms < LEGACY_ADOPTION_GRACE_MS
        or state.get("legacy_orphan_cleanup_done_ms")
    ):
        return [], []
    discover = getattr(controller, "orphan_mcp_roots", None)
    if not callable(discover):
        return [], []
    codex_pid = int(state.get("codex_pid") or 0)
    try:
        roots = tuple(discover(codex_pid))
    except Exception:
        return [], []
    if not roots:
        return [], []

    protected_root_pids = {
        process.pid
        for cohort in cohorts
        for process in cohort.roots
    }
    fresh = controller.snapshot()
    if fresh is None:
        return [], [root.pid for root in roots]

    killed: list[int] = []
    failed: list[int] = []
    for root in roots:
        if (
            root.pid == self_pid
            or root.parent_pid != codex_pid
            or root.pid in protected_root_pids
            or root.start_ms >= started_ms
            or not _same_root(root, fresh)
        ):
            continue
        if controller.terminate_tree(root):
            killed.append(root.pid)
        else:
            failed.append(root.pid)
    if killed and not failed:
        state["legacy_orphan_cleanup_done_ms"] = now_ms
    return killed, failed


def _cleanup(
    controller: ProcessController,
    state: dict[str, Any],
    now_ms: int,
    self_pid: int,
    mode: str,
) -> tuple[list[int], list[int], int, list[int]]:
    snapshot = controller.snapshot()
    if snapshot is None:
        return [], [], 0, []
    cohorts = _cohorts(snapshot, self_pid)
    by_key = {c.key: c for c in cohorts}
    owned = _owned(state)
    retired = state.setdefault("retired", {})
    native_cleanup_count = 0

    for key in list(retired):
        if key in owned:
            retired.pop(key, None)
        elif key not in by_key:
            retired.pop(key, None)
            native_cleanup_count += 1

    # Unknown pre-shim cohorts have no trustworthy ownership proof. Automatic
    # mode must never turn age into authority; only explicit diagnostic force
    # mode may drain the legacy quarantine.
    if mode == "force":
        _retire_expired_legacy(state, by_key, owned, now_ms)

    targets: dict[str, Cohort] = {}
    for key, receipt in list(retired.items()):
        cohort = by_key.get(key)
        if cohort is None or key in owned:
            continue
        if mode == "force" or now_ms - int((receipt or {}).get("retired_ms") or now_ms) >= NATIVE_CLEANUP_GRACE_MS:
            targets[key] = cohort

    reclaim_candidate_pids = sorted(
        {
            process.pid
            for cohort in targets.values()
            for process in cohort.roots
            if process.pid != self_pid
        }
    )

    # Generic auto observation never turns age or an unmatched replacement
    # into a kill. The only automatic termination seam here is an exclusive
    # writer-lock-superseded leftover that survived native cleanup grace and
    # still matches PID/parent/name/start identity.
    kill_targets: dict[str, Cohort] = {}
    if mode == "force":
        kill_targets = targets
    elif mode == "auto":
        for key, cohort in targets.items():
            reason = str((retired.get(key) or {}).get("reason") or "")
            if reason != "writer_lock_superseded":
                continue
            if key in owned or key in _owned(state):
                continue
            kill_targets[key] = cohort

    if not kill_targets:
        return [], [], native_cleanup_count, reclaim_candidate_pids

    killed: list[int] = []
    failed: list[int] = []
    for key, cohort in sorted(kill_targets.items(), key=lambda item: item[1].anchor_start_ms):
        just_killed, just_failed = _terminate(controller, cohort, self_pid)
        killed.extend(just_killed)
        failed.extend(just_failed)
        if just_killed and not just_failed:
            retired.pop(key, None)
        elif just_failed:
            receipt = retired.setdefault(key, {})
            receipt.setdefault("retired_ms", now_ms)
            receipt["last_attempt_ms"] = now_ms
            receipt.setdefault("reason", "cleanup_retry")
    return killed, failed, native_cleanup_count, reclaim_candidate_pids


def _locked_handle(
    event: str,
    path: Path,
    thread_id: str,
    controller: ProcessController,
    now_ms: int,
    self_pid: int,
    mode: str,
    *,
    host_thread_id: str = "",
    initial_snapshot: ProcessSnapshot | None = None,
    legacy_path: Path | None = None,
) -> dict[str, Any]:
    if event == "post_tool" and mode == "auto":
        hint = _load_state(path)
        lease = (hint.get("threads") or {}).get(thread_id) or {}
        if lease and now_ms - int(hint.get("last_probe_ms") or 0) < POST_TOOL_PROBE_INTERVAL_MS:
            lease["last_seen_ms"] = now_ms
            if host_thread_id:
                lease["host_thread_id"] = host_thread_id
            lease["pulse_count"] = int(lease.get("pulse_count") or 0) + 1
            lease["last_observed_cohort_count"] = int(
                hint.get("last_cohort_count") or 0
            )
            assignment_reason = str(lease.get("assignment_reason") or "")
            hint.update(
                updated_ms=now_ms,
                last_event=event,
                last_thread_id=thread_id,
                last_assignment_reason=assignment_reason,
            )
            try:
                _save_state(path, hint)
            except OSError:
                pass
            return {
                "status": "ok",
                "mode": mode,
                "event": event,
                "action": "throttled",
                "cohort_count": int(hint.get("last_cohort_count") or 0),
                "assigned_cohort": str(lease.get("cohort_key") or ""),
                "assignment_reason": assignment_reason,
                "native_cleanup_count": 0,
                "termination_enabled": mode == "force",
                "reclaim_candidate_pids": list(
                    hint.get("last_reclaim_candidate_pids") or []
                ),
                "killed_pids": [],
                "failed_pids": [],
            }

    snapshot = initial_snapshot or controller.snapshot()
    if snapshot is None:
        return {"status": "degraded", "reason": "process_snapshot_unavailable", "mode": mode}
    cohorts = _cohorts(snapshot, self_pid)
    state = _prepare_state(path, snapshot, legacy_path=legacy_path)
    _prune_leases(
        state,
        now_ms,
        {cohort.key for cohort in cohorts},
    )
    reserved_delta = (
        _unique_snapshot_delta(state, cohorts, thread_id)
        if event in LEASE_EVENTS
        else None
    )
    writer_stats = _reconcile_writer_evidence(
        state,
        cohorts,
        snapshot,
        now_ms,
        reserved_cohort_key=(reserved_delta.key if reserved_delta else ""),
    )
    has_writer_evidence = writer_stats["evidence_count"] > 0
    threads = state.setdefault("threads", {})
    retired = state.setdefault("retired", {})

    assigned = ""
    assignment_reason = ""
    if event in LEASE_EVENTS:
        previous = threads.get(thread_id) or {}
        previous_key = str(previous.get("cohort_key") or "")
        ambiguous_empty_delta = _has_snapshot_delta(state, cohorts) and not previous_key
        candidate = reserved_delta or _unique_snapshot_delta(
            state,
            cohorts,
            thread_id,
        )
        if candidate is not None:
            assignment_reason = "snapshot_delta"
        elif not ambiguous_empty_delta:
            writer_owned_by_other = (
                _owned(state, excluding_thread=thread_id)
                if has_writer_evidence
                else set()
            )
            candidate = _nearest(
                tuple(
                    cohort
                    for cohort in cohorts
                    if cohort.key not in retired
                    and cohort.key not in writer_owned_by_other
                ),
                now_ms,
            )
            if candidate is not None:
                assignment_reason = "nearest_start"
        if (
            candidate is None
            and not ambiguous_empty_delta
            and not has_writer_evidence
        ):
            _seed_legacy_adoption(state, cohorts, now_ms)
            previous = threads.get(thread_id) or previous
            previous_key = str(previous.get("cohort_key") or previous_key)
        if candidate is None and not ambiguous_empty_delta and previous_key:
            candidate = next((c for c in cohorts if c.key == previous_key), None)
            if candidate is not None:
                assignment_reason = "existing_lease"
        if candidate is None and not ambiguous_empty_delta:
            candidate = _reusable_retired(
                cohorts,
                state,
                thread_id,
                strict_thread=has_writer_evidence,
            )
            if candidate is not None:
                assignment_reason = "retired_reuse"
        if (
            candidate is None
            and not ambiguous_empty_delta
            and not has_writer_evidence
        ):
            candidate = _claim_legacy_candidate(state, cohorts)
            if candidate is not None:
                assignment_reason = "legacy_adoption"
        if (
            candidate is None
            and not ambiguous_empty_delta
            and not has_writer_evidence
        ):
            candidate = _unique_unowned_cohort(cohorts, state)
            if candidate is not None:
                assignment_reason = "unique_unowned"
        assigned = candidate.key if candidate else ""
        if (
            not assigned
            and previous_key
            and any(cohort.key == previous_key for cohort in cohorts)
        ):
            # Writer-lock / snapshot-delta ownership survives a later pulse that
            # cannot uniquely re-guess the cohort, including when host_thread_id
            # is inherited/shared with another lease.
            assigned = previous_key
            assignment_reason = str(
                previous.get("assignment_reason") or assignment_reason or "existing_lease"
            )
        if previous_key and previous_key != assigned and previous_key not in _owned(state, thread_id):
            _retire(state, previous_key, now_ms, "replaced_by_new_cohort", thread_id)
        previous_reason = str(previous.get("assignment_reason") or "")
        effective_reason = (
            previous_reason
            if previous_key and previous_key == assigned and previous_reason
            else assignment_reason
        )
        threads[thread_id] = {
            "turn_started_ms": int(previous.get("turn_started_ms") or now_ms),
            "last_seen_ms": now_ms,
            "cohort_key": assigned,
            "turn_state": "active",
            "assignment_reason": effective_reason,
            "pulse_count": int(previous.get("pulse_count") or 0) + 1,
            "last_observed_cohort_count": len(cohorts),
            "host_thread_id": host_thread_id or str(previous.get("host_thread_id") or ""),
        }
        assignment_reason = effective_reason
        if assigned:
            retired.pop(assigned, None)
    else:
        # Codex Stop is a turn boundary, not a conversation/thread close. Keep
        # the transport lease so the next resumed turn does not inherit a
        # deliberately severed MCP connection.
        lease = threads.get(thread_id) or {
            "turn_started_ms": now_ms,
            "cohort_key": "",
        }
        assigned = str(lease.get("cohort_key") or "")
        lease["last_seen_ms"] = now_ms
        lease["last_stop_ms"] = now_ms
        lease["turn_state"] = "idle"
        if host_thread_id:
            lease["host_thread_id"] = host_thread_id
        threads[thread_id] = lease
        assignment_reason = "turn_stop_preserved"

    if not has_writer_evidence:
        _reconcile_unique_empty_lease(state, cohorts)
        refreshed = threads.get(thread_id) or {}
        refreshed_key = str(refreshed.get("cohort_key") or "")
        if not assigned and refreshed_key:
            assigned = refreshed_key
            assignment_reason = str(
                refreshed.get("assignment_reason")
                or "unique_unowned_reconcile"
            )
            refreshed["assignment_reason"] = assignment_reason
            refreshed["last_observed_cohort_count"] = len(cohorts)
    (
        killed,
        failed,
        native_cleanup_count,
        reclaim_candidate_pids,
    ) = _cleanup(controller, state, now_ms, self_pid, mode)
    orphan_killed: list[int] = []
    orphan_failed: list[int] = []
    if mode == "force":
        orphan_killed, orphan_failed = _cleanup_legacy_orphan_roots(
            controller,
            state,
            cohorts,
            now_ms,
            self_pid,
        )
    killed.extend(orphan_killed)
    failed.extend(orphan_failed)
    state.update(
        shim_version=SHIM_VERSION,
        updated_ms=now_ms,
        last_event=event,
        last_thread_id=thread_id,
        last_assignment_reason=assignment_reason,
        last_killed_pids=sorted(set(killed)),
        last_failed_pids=sorted(set(failed)),
        last_native_cleanup_count=native_cleanup_count,
        last_reclaim_candidate_pids=reclaim_candidate_pids,
        termination_enabled=mode == "force" or bool(killed),
        last_probe_ms=now_ms,
        last_cohort_count=len(cohorts),
        observed_cohort_keys=sorted(cohort.key for cohort in cohorts),
    )
    try:
        _save_state(path, state)
    except OSError:
        pass
    action = (
        "reclaimed"
        if killed
        else "reclaim_pending"
        if reclaim_candidate_pids
        else "observing"
    )
    return {
        "status": "ok" if not failed else "degraded",
        "mode": mode,
        "event": event,
        "action": action,
        "cohort_count": len(cohorts),
        "assigned_cohort": assigned,
        "assignment_reason": assignment_reason,
        "native_cleanup_count": native_cleanup_count,
        "termination_enabled": mode == "force" or bool(killed),
        "reclaim_candidate_pids": reclaim_candidate_pids,
        "killed_pids": sorted(set(killed)),
        "failed_pids": sorted(set(failed)),
    }


def reclaim_terminal_codex_threads(
    *,
    workspace: str | Path,
    thread_ids: Iterable[str],
    controller: ProcessController | None = None,
    now_ms: int | None = None,
    self_pid: int | None = None,
) -> dict[str, Any]:
    """Reclaim only cohorts proven exclusive to host-terminal Codex threads.

    This is intentionally separate from normal ``auto`` lifecycle handling.
    The caller must already have hard host evidence that the supplied thread
    ids are terminal (for example a reconciled ``SubagentStop`` or a deleted /
    archived child thread).  Ambiguous or shared cohorts remain untouched.
    """

    normalized = {
        str(value or "").strip()
        for value in thread_ids
        if str(value or "").strip()
    }
    if not normalized:
        return {
            "status": "skipped",
            "reason": "terminal_thread_ids_missing",
            "killed_pids": [],
            "failed_pids": [],
        }
    if os.name != "nt" and controller is None:
        return {
            "status": "skipped",
            "reason": "non_windows",
            "killed_pids": [],
            "failed_pids": [],
        }

    active_controller = controller or WindowsProcessController(allow_termination=True)
    snapshot = active_controller.snapshot()
    if snapshot is None:
        return {
            "status": "degraded",
            "reason": "process_snapshot_unavailable",
            "killed_pids": [],
            "failed_pids": [],
        }
    current_pid = int(self_pid if self_pid is not None else os.getpid())
    cohorts = _cohorts(snapshot, current_pid)
    by_key = {cohort.key: cohort for cohort in cohorts}
    evidence = _read_thread_lock_evidence(snapshot)
    exact_matches = {
        item.thread_id: cohort
        for cohort, item in _exact_writer_matches(cohorts, evidence)
    }

    root = Path(workspace).expanduser().resolve()
    path = _state_path(root, snapshot)
    legacy_path = _state_index_path(root)
    timestamp = int(now_ms if now_ms is not None else time.time() * 1000)
    try:
        with _state_lock(path):
            state = _prepare_state(path, snapshot, legacy_path=legacy_path)
            threads = state.setdefault("threads", {})
            owners_by_cohort: dict[str, set[str]] = {}
            for lease_id, lease in threads.items():
                cohort_key = str((lease or {}).get("cohort_key") or "")
                if cohort_key:
                    owners_by_cohort.setdefault(cohort_key, set()).add(str(lease_id))

            killed: list[int] = []
            failed: list[int] = []
            reclaimed_threads: list[str] = []
            skipped_shared: list[str] = []
            skipped_ambiguous: list[str] = []
            terminal_aliases: dict[str, set[str]] = {}

            for thread_id in sorted(normalized):
                digest = hashlib.sha256(
                    thread_id.encode("utf-8", errors="replace")
                ).hexdigest()[:24]
                aliases = {
                    f"thread:{thread_id}",
                    f"session:{digest}",
                }
                terminal_aliases[thread_id] = aliases
                candidate_keys: set[str] = set()

                exact = exact_matches.get(thread_id)
                if exact is not None:
                    candidate_keys.add(exact.key)
                    aliases.add(
                        next(
                            (
                                item.lease_id
                                for item in evidence
                                if item.thread_id == thread_id
                            ),
                            f"session:{digest}",
                        )
                    )
                for alias in aliases:
                    lease = threads.get(alias) or {}
                    cohort_key = str(lease.get("cohort_key") or "")
                    if cohort_key in by_key:
                        candidate_keys.add(cohort_key)
                for lease_id, lease in threads.items():
                    if not isinstance(lease, dict):
                        continue
                    if str(lease.get("host_thread_id") or "").strip() != thread_id:
                        continue
                    aliases.add(str(lease_id))
                    cohort_key = str(lease.get("cohort_key") or "")
                    if (
                        cohort_key in by_key
                        and str(lease.get("turn_state") or "") != "active"
                    ):
                        candidate_keys.add(cohort_key)

                if len(candidate_keys) != 1:
                    skipped_ambiguous.append(thread_id)
                    continue
                cohort_key = next(iter(candidate_keys))
                owners = owners_by_cohort.get(cohort_key, set())
                if owners - aliases:
                    skipped_shared.append(thread_id)
                    continue
                cohort = by_key.get(cohort_key)
                if cohort is None:
                    continue

                just_killed, just_failed = _terminate(
                    active_controller,
                    cohort,
                    current_pid,
                )
                killed.extend(just_killed)
                failed.extend(just_failed)
                if just_failed or not just_killed:
                    continue

                reclaimed_threads.append(thread_id)
                for alias in aliases:
                    lease = threads.get(alias)
                    if isinstance(lease, dict):
                        lease["cohort_key"] = ""
                        lease["terminal_reclaimed_ms"] = timestamp
                state.setdefault("retired", {}).pop(cohort_key, None)
                owners_by_cohort.pop(cohort_key, None)

            state.update(
                shim_version=SHIM_VERSION,
                updated_ms=timestamp,
                last_terminal_reclaim_ms=timestamp,
                last_terminal_reclaimed_thread_count=len(reclaimed_threads),
                last_terminal_killed_pids=sorted(set(killed)),
                last_terminal_failed_pids=sorted(set(failed)),
            )
            _save_state(path, state)
            _publish_state_index(root, path, state)
            return {
                "status": "ok" if not failed else "degraded",
                "reason": "terminal_reclaim",
                "reclaimed_thread_ids": reclaimed_threads,
                "skipped_shared_thread_ids": skipped_shared,
                "skipped_ambiguous_thread_ids": skipped_ambiguous,
                "killed_pids": sorted(set(killed)),
                "failed_pids": sorted(set(failed)),
                "generation_key": _generation_key(snapshot),
            }
    except (OSError, TimeoutError) as exc:
        return {
            "status": "degraded",
            "reason": "state_lock_unavailable",
            "detail": type(exc).__name__,
            "killed_pids": [],
            "failed_pids": [],
        }


def reclaim_indexed_terminal_codex_threads(
    *,
    workspace: str | Path,
    protected_thread_ids: Iterable[str] = (),
    controller: ProcessController | None = None,
    now_ms: int | None = None,
    self_pid: int | None = None,
) -> dict[str, Any]:
    """Reclaim idle leases whose verified Codex thread was archived or deleted."""

    protected = {
        str(value or "").strip()
        for value in protected_thread_ids
        if str(value or "").strip()
    }
    if os.name != "nt" and controller is None:
        return {"status": "skipped", "reason": "non_windows", "killed_pids": []}
    active_controller = controller or WindowsProcessController(allow_termination=True)
    snapshot = active_controller.snapshot()
    if snapshot is None:
        return {
            "status": "degraded",
            "reason": "process_snapshot_unavailable",
            "killed_pids": [],
        }

    root = Path(workspace).expanduser().resolve()
    state = _load_state(_state_path(root, snapshot))
    threads = state.get("threads") if isinstance(state.get("threads"), dict) else {}
    candidates: set[str] = set()
    for lease in threads.values():
        if not isinstance(lease, dict):
            continue
        host_thread_id = str(lease.get("host_thread_id") or "").strip()
        if (
            not host_thread_id
            or host_thread_id in protected
            or str(lease.get("turn_state") or "") != "idle"
            or not str(lease.get("cohort_key") or "")
        ):
            continue
        candidates.add(host_thread_id)
    if not candidates:
        return {
            "status": "ok",
            "reason": "no_indexed_idle_candidates",
            "terminal_thread_ids": [],
            "killed_pids": [],
            "failed_pids": [],
        }

    db_path = _codex_home() / "state_5.sqlite"
    if not db_path.is_file():
        return {
            "status": "degraded",
            "reason": "state_db_missing",
            "terminal_thread_ids": [],
            "killed_pids": [],
            "failed_pids": [],
        }
    placeholders = ",".join("?" for _ in candidates)
    try:
        connection = sqlite3.connect(
            db_path.as_uri() + "?mode=ro",
            uri=True,
            timeout=0.25,
        )
        rows = connection.execute(
            "SELECT id,archived FROM threads WHERE id IN (" + placeholders + ")",
            tuple(sorted(candidates)),
        ).fetchall()
    except sqlite3.Error:
        return {
            "status": "degraded",
            "reason": "state_db_read_failed",
            "terminal_thread_ids": [],
            "killed_pids": [],
            "failed_pids": [],
        }
    finally:
        try:
            connection.close()
        except (NameError, sqlite3.Error):
            pass

    indexed = {str(row[0]): bool(row[1]) for row in rows}
    terminal = sorted(
        thread_id
        for thread_id in candidates
        if thread_id not in indexed or indexed[thread_id]
    )
    if not terminal:
        return {
            "status": "ok",
            "reason": "indexed_threads_live",
            "terminal_thread_ids": [],
            "killed_pids": [],
            "failed_pids": [],
        }
    result = reclaim_terminal_codex_threads(
        workspace=root,
        thread_ids=terminal,
        controller=active_controller,
        now_ms=now_ms,
        self_pid=self_pid,
    )
    result["terminal_thread_ids"] = terminal
    result["indexed_terminal_reclaim"] = True
    return result


def handle_codex_mcp_lifecycle(
    *,
    event: str,
    workspace: str | Path,
    thread_id: str,
    host_thread_id: str = "",
    controller: ProcessController | None = None,
    now_ms: int | None = None,
    self_pid: int | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Observe one host event and reclaim only proven Codex leftovers."""
    selected_mode = _mode(mode)
    if selected_mode == "off":
        return {"status": "skipped", "reason": "disabled", "mode": selected_mode}
    if event not in SUPPORTED_EVENTS:
        return {"status": "skipped", "reason": "unsupported_event", "mode": selected_mode}
    if not thread_id:
        return {"status": "skipped", "reason": "missing_trusted_thread_id", "mode": selected_mode}
    if os.name != "nt" and controller is None:
        return {"status": "skipped", "reason": "non_windows", "mode": selected_mode}

    active_controller = controller or WindowsProcessController(
        allow_termination=selected_mode in {"auto", "force"}
    )
    timestamp = int(now_ms if now_ms is not None else time.time() * 1000)
    current_pid = int(self_pid if self_pid is not None else os.getpid())
    root = Path(workspace).expanduser().resolve()
    try:
        snapshot = active_controller.snapshot()
    except Exception as exc:
        return {
            "status": "degraded",
            "reason": "process_snapshot_unavailable",
            "detail": type(exc).__name__,
            "mode": selected_mode,
        }
    if snapshot is None:
        return {
            "status": "degraded",
            "reason": "process_snapshot_unavailable",
            "mode": selected_mode,
        }

    path = _state_path(root, snapshot)
    legacy_path = _state_index_path(root)
    try:
        with _state_lock(path):
            result = _locked_handle(
                event,
                path,
                thread_id,
                active_controller,
                timestamp,
                current_pid,
                selected_mode,
                host_thread_id=str(host_thread_id or "").strip(),
                initial_snapshot=snapshot,
                legacy_path=legacy_path,
            )
    except (OSError, TimeoutError) as exc:
        return {
            "status": "degraded",
            "reason": "state_lock_unavailable",
            "detail": type(exc).__name__,
            "mode": selected_mode,
        }

    state = _load_state(path)
    _publish_state_index(root, path, state)
    result["generation_key"] = _generation_key(snapshot)
    result["state_path"] = str(path)
    return result


__all__ = [
    "Cohort",
    "LIFECYCLE_ENV",
    "ProcessController",
    "ProcessInfo",
    "ProcessSnapshot",
    "SHIM_VERSION",
    "ThreadLockEvidence",
    "WindowsProcessController",
    "handle_codex_mcp_lifecycle",
    "reclaim_indexed_terminal_codex_threads",
    "reclaim_terminal_codex_threads",
]
