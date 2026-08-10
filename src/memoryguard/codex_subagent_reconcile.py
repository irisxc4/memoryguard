"""Safe reconciliation for stale Codex sub-agent state.

Codex keeps the UI index in ``state_5.sqlite``.  A stopped child can leave an
``open`` row in ``thread_spawn_edges`` even though the child is no longer
running, which makes the parent appear to have work in progress forever.
Root-scoped reconciliation is driven by the trusted Stop hook.  Global crash
recovery is stricter: it reads only the final structured event of an existing
rollout and closes a branch only when every open descendant has an explicit
terminal event.  Rollout files are never modified and prompt/response bodies
are never persisted in diagnostics.

The module is intentionally defensive because the database is owned by Codex:

* root-scoped repair traverses only an explicitly supplied root/thread id;
* global repair requires an explicit terminal rollout event and an active-ID
  exclusion boundary;
* the database must already exist and pass a schema preflight;
* every mutation is one SQLite transaction with a bounded busy timeout;
* a Connection.backup() snapshot is made before a mutation (and at most once
  per day when there is no mutation), retaining the newest three snapshots;
* failures return a JSON-safe degraded result and write a small diagnostic
  receipt instead of raising into a host hook.

``CODEX_THREAD_ID`` is trusted only when supplied by the host environment.  A
prompt or arbitrary payload is never used as the implicit root id.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from typing import Any, Iterable, Mapping


RECONCILE_VERSION = "2"
DEFAULT_DB_NAME = "state_5.sqlite"
DEFAULT_BACKUP_DIR_NAME = "memoryguard-codex-backups"
DEFAULT_RECEIPT_DIR_NAME = "memoryguard-reconcile-receipts"
DEFAULT_BACKUP_RETENTION = 3
DEFAULT_BUSY_TIMEOUT_MS = 1000
MAX_DESCENDANTS = 50_000
MAX_ROLLOUT_TAIL_BYTES = 256 * 1024
TERMINAL_ROLLOUT_EVENTS = frozenset({"task_complete", "turn_aborted"})

# Codex ids are UUIDs today, but tests and older installations may use opaque
# ids.  Reject path/control characters while accepting a conservative opaque
# identifier set.  The value is always bound as a SQL parameter too.
_TRUSTED_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_BACKUP_RE = re.compile(r"^state_5\.sqlite\.mg-(\d{8}T\d{6}\.\d{6}Z)\.bak$")


class _ReconcileError(RuntimeError):
    """Expected, fail-silent reconciliation failure."""

    def __init__(self, reason: str, *, status: str = "degraded") -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if _TRUSTED_ID_RE.fullmatch(text) else ""


def trusted_codex_thread_id(environ: Mapping[str, Any] | None = None) -> str:
    """Return only a host-provided, syntactically safe Codex thread id.

    The default source is the process environment.  A mapping argument exists
    for deterministic tests; callers must not pass prompt payloads here.
    """

    source = os.environ if environ is None else environ
    return _safe_id(source.get("CODEX_THREAD_ID", ""))


def resolve_codex_home(codex_home: str | Path | None = None) -> Path:
    """Resolve the Codex home without creating it."""

    if codex_home is None:
        configured = os.environ.get("CODEX_HOME", "").strip()
        codex_home = configured or (Path.home() / ".codex")
    return Path(codex_home).expanduser().resolve(strict=False)


def resolve_state_db_path(
    state_db_path: str | Path | None = None,
    *,
    codex_home: str | Path | None = None,
) -> Path:
    """Resolve the state database path (without opening or creating it)."""

    if state_db_path is None:
        return resolve_codex_home(codex_home) / DEFAULT_DB_NAME
    return Path(state_db_path).expanduser().resolve(strict=False)


def _normalized_fs_path(value: str | Path) -> str:
    text = str(Path(value).expanduser().resolve(strict=False))
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return os.path.normcase(text.rstrip("\\/"))


def codex_thread_matches_workspace(
    thread_id: str,
    workspace: str | Path,
    *,
    state_db_path: str | Path | None = None,
    codex_home: str | Path | None = None,
) -> bool:
    """Authenticate a host hook by matching its thread cwd to the workspace."""

    trusted = _safe_id(thread_id)
    home = resolve_codex_home(codex_home)
    db_path = resolve_state_db_path(state_db_path, codex_home=home)
    if not trusted or not _is_within(db_path, home) or not db_path.is_file():
        return False
    try:
        conn = sqlite3.connect(
            f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=0.25
        )
        try:
            if "cwd" not in _table_columns(conn, "threads"):
                return False
            row = conn.execute(
                "SELECT cwd FROM threads WHERE id=?", (trusted,)
            ).fetchone()
        finally:
            conn.close()
        if row is None or not isinstance(row[0], str) or not row[0].strip():
            return False
        thread_cwd = _normalized_fs_path(row[0])
        hook_workspace = _normalized_fs_path(workspace)
        separator = os.sep
        return (
            thread_cwd == hook_workspace
            or thread_cwd.startswith(hook_workspace + separator)
            or hook_workspace.startswith(thread_cwd + separator)
        )
    except (OSError, sqlite3.Error, ValueError):
        return False


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _base_result(
    *,
    root_thread_id: str,
    db_path: Path,
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "version": RECONCILE_VERSION,
        "provider": "codex",
        "root_thread_id": root_thread_id,
        "state_db_path": str(db_path),
        "dry_run": bool(dry_run),
        "ok": False,
        "degraded": False,
        "changed": False,
        "status": "pending",
        "reason": "",
        "closed_edge_ids": [],
        "closed_edges": [],
        "archived_thread_ids": [],
        "candidate_edge_ids": [],
        "candidate_thread_ids": [],
        "skipped_active_thread_ids": [],
        "missing_thread_ids": [],
        "active_thread_ids": [],
        "active_whitelist": [],
        "closed_edge_count": 0,
        "archived_thread_count": 0,
        "backup_path": "",
        "backup_paths": [],
        "restore_path": "",
        "diagnostic_receipt": "",
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return value


def _id_digest(value: Any) -> str:
    """Return a short, non-reversible identifier digest for diagnostics."""
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def sanitize_reconcile_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Build the disk-safe subset of a public reconciliation result.

    Public callers may need the absolute restore path, but persistent Hook
    receipts must not become a filesystem or thread-id inventory.  Keep only
    bounded status/count metadata, stable path labels, and irreversible ID
    digests.  This helper is deliberately tolerant of monkeypatched/older
    result shapes so a degraded Hook still writes a useful receipt.
    """
    def _count(name: str | None, fallback: str) -> int:
        raw = result.get(name) if name else None
        if isinstance(raw, int) and raw >= 0:
            return raw
        values = result.get(fallback, ())
        return len(values) if isinstance(values, (list, tuple, set)) else 0

    root_digest = _id_digest(result.get("root_thread_id"))
    active_values = result.get("active_thread_ids", ())
    active_digests = sorted(
        digest
        for digest in (_id_digest(item) for item in (active_values or ()))
        if digest
    )
    safe: dict[str, Any] = {
        "version": str(result.get("version") or RECONCILE_VERSION),
        "provider": "codex",
        "status": str(result.get("status") or "degraded"),
        "ok": bool(result.get("ok")),
        "degraded": bool(result.get("degraded")),
        "reason": str(result.get("reason") or "")[:240],
        "changed": bool(result.get("changed")),
        "dry_run": bool(result.get("dry_run")),
        "global_reconcile": bool(result.get("global_reconcile")),
        "closed_edge_count": _count("closed_edge_count", "closed_edge_ids"),
        "archived_thread_count": _count(
            "archived_thread_count", "archived_thread_ids"
        ),
        "candidate_edge_count": _count(None, "candidate_edge_ids"),
        "candidate_thread_count": _count(None, "candidate_thread_ids"),
        "skipped_active_count": _count(None, "skipped_active_thread_ids"),
        "missing_thread_count": _count(None, "missing_thread_ids"),
        "state_db_label": "codex_home/state_5.sqlite",
        "backup_path_label": "codex_home/memoryguard-codex-backups/state_5.sqlite",
        "backup_created": bool(result.get("backup_path")),
        "backup_retained_count": _count(None, "backup_paths"),
        "restore_available": bool(result.get("restore_path")),
    }
    for name in (
        "open_edge_count",
        "missing_thread_count",
        "skipped_nonterminal_count",
    ):
        value = result.get(name)
        if isinstance(value, int) and value >= 0:
            safe[name] = value
    event_counts = result.get("terminal_event_counts")
    if isinstance(event_counts, Mapping):
        safe["terminal_event_counts"] = {
            str(name): int(count)
            for name, count in event_counts.items()
            if str(name) in TERMINAL_ROLLOUT_EVENTS | {"archived"}
            and isinstance(count, int)
            and count >= 0
        }
    if root_digest:
        safe["root_thread_digest"] = root_digest
    if active_digests:
        safe["active_thread_digests"] = active_digests
    return safe


def _write_receipt(
    result: Mapping[str, Any],
    receipt_dir: Path,
) -> str:
    """Write a compact JSON receipt; never include rollout/prompt contents."""

    try:
        receipt_dir = receipt_dir.expanduser().resolve(strict=False)
        receipt_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        path = receipt_dir / f"codex-reconcile-{stamp}-{os.getpid()}.json"
        payload = sanitize_reconcile_result(result)
        payload["recorded_at"] = datetime.now(timezone.utc).isoformat()
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(receipt_dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=True, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_name, path)
        finally:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass
        return str(path)
    except Exception:
        return ""


def _configure_connection(conn: sqlite3.Connection, busy_timeout_ms: int) -> None:
    timeout = max(1, min(int(busy_timeout_ms), 30_000))
    conn.execute(f"PRAGMA busy_timeout = {timeout}")
    # A read-only integrity check catches many truncated/corrupt snapshots
    # before a write transaction is attempted.  It does not modify the DB.
    try:
        check = conn.execute("PRAGMA quick_check").fetchone()
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
            raise _ReconcileError("state_db_locked", status="locked") from exc
        raise
    if check and str(check[0]).lower() != "ok":
        raise _ReconcileError("state_db_integrity_check_failed", status="corrupt")


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    # Table names are constants, never caller input.
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _schema_preflight(conn: sqlite3.Connection) -> dict[str, set[str]]:
    tables = {
        "threads": _table_columns(conn, "threads"),
        "thread_spawn_edges": _table_columns(conn, "thread_spawn_edges"),
    }
    required = {
        # updated_at/recency_at are required because the parent recency bump is
        # what makes Codex refresh its panel after the transaction commits.
        "threads": {"id", "archived", "archived_at", "updated_at", "recency_at"},
        "thread_spawn_edges": {"parent_thread_id", "child_thread_id", "status"},
    }
    missing = {
        table: sorted(required[table] - columns)
        for table, columns in tables.items()
        if required[table] - columns
    }
    if missing:
        raise _ReconcileError(
            "state_db_schema_mismatch:" + json.dumps(missing, sort_keys=True),
            status="schema_mismatch",
        )
    return tables


def _backup_name(now_ms: int) -> str:
    stamp = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime(
        "%Y%m%dT%H%M%S.%fZ"
    )
    return f"{DEFAULT_DB_NAME}.mg-{stamp}.bak"


def _backup_files(backup_dir: Path) -> list[Path]:
    if not backup_dir.exists():
        return []
    files = [
        path
        for path in backup_dir.iterdir()
        if path.is_file() and _BACKUP_RE.fullmatch(path.name)
    ]
    return sorted(files, key=lambda item: (item.stat().st_mtime_ns, item.name), reverse=True)


def _prune_backups(backup_dir: Path, retention: int) -> list[Path]:
    # Safety contract is hard-bounded to three snapshots even if an
    # integration passes a larger retention value.
    keep = min(DEFAULT_BACKUP_RETENTION, max(1, int(retention)))
    files = _backup_files(backup_dir)
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            # A concurrently held backup is retained; never let retention
            # housekeeping compromise the state DB reconciliation.
            continue
    return _backup_files(backup_dir)


def _has_backup_today(files: Iterable[Path], now_ms: int) -> bool:
    today = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y%m%d")
    return any(path.name.startswith(f"{DEFAULT_DB_NAME}.mg-{today}T") for path in files)


def _online_backup(
    source: sqlite3.Connection,
    *,
    db_path: Path,
    backup_dir: Path,
    now_ms: int,
    retention: int,
) -> tuple[Path, list[Path]]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / _backup_name(now_ms)
    # mkstemp creates a file; sqlite3 must open it as a database.  Closing the
    # descriptor first avoids Windows sharing violations.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(backup_dir)
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        destination = sqlite3.connect(str(tmp), timeout=1.0)
        try:
            source.backup(destination, pages=256, sleep=0.05)
            destination.commit()
        finally:
            destination.close()
        os.replace(tmp, target)
    except sqlite3.Error as exc:
        raise _ReconcileError(f"state_db_backup_failed:{type(exc).__name__}") from exc
    except OSError as exc:
        raise _ReconcileError(f"state_db_backup_failed:{type(exc).__name__}") from exc
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    retained = _prune_backups(backup_dir, retention)
    return target, retained


def _active_ids(value: Iterable[Any] | None) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (str, bytes)):
        value = [value]
    result: set[str] = set()
    for item in value:
        normalized = _safe_id(item)
        if normalized:
            result.add(normalized)
    return result


def _read_active_ids_from_env() -> set[str]:
    raw = os.environ.get("CODEX_ACTIVE_THREAD_IDS", "")
    return _active_ids(part.strip() for part in raw.split(",") if part.strip())


def _terminal_rollout_event(raw_path: Any, *, codex_home: Path) -> str:
    """Return one allow-listed terminal event without exposing rollout text."""

    if not isinstance(raw_path, str) or not raw_path.strip():
        return ""
    path = Path(raw_path).expanduser()
    if not path.is_absolute() or path.is_symlink():
        return ""
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return ""
    if not _is_within(path, codex_home) or not path.is_file():
        return ""
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - MAX_ROLLOUT_TAIL_BYTES))
            lines = stream.read(MAX_ROLLOUT_TAIL_BYTES).decode(
                "utf-8", errors="replace"
            ).splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(item, Mapping):
            continue
        payload = item.get("payload")
        event = str(payload.get("type") or "") if isinstance(payload, Mapping) else ""
        if str(item.get("type") or "") == "event_msg" and event in TERMINAL_ROLLOUT_EVENTS:
            return event
        # The last structured event is authoritative.  A non-terminal event
        # after an older task_complete means the task may have resumed.
        return ""
    return ""


def _global_terminal_graph(
    conn: sqlite3.Connection,
    *,
    codex_home: Path,
    active_thread_ids: set[str],
) -> dict[str, Any]:
    """Find globally stale branches using terminal rollout evidence only."""

    if "rollout_path" not in _table_columns(conn, "threads"):
        raise _ReconcileError(
            "state_db_rollout_path_missing", status="schema_mismatch"
        )
    rows = conn.execute(
        "SELECT e.parent_thread_id, e.child_thread_id, e.status, "
        "t.archived, t.rollout_path "
        "FROM thread_spawn_edges e "
        "LEFT JOIN threads t ON t.id=e.child_thread_id"
    ).fetchall()
    if len(rows) > MAX_DESCENDANTS:
        raise _ReconcileError("global_edge_limit_exceeded", status="bounded")

    open_rows: list[tuple[str, str, str, Any, Any]] = []
    open_children: dict[str, set[str]] = defaultdict(set)
    terminal: dict[str, str] = {}
    skipped_active: set[str] = set()
    missing_threads = 0
    skipped_nonterminal = 0

    for raw_parent, raw_child, raw_status, archived, rollout_path in rows:
        parent = _safe_id(raw_parent)
        child = _safe_id(raw_child)
        status = str(raw_status or "")
        if not parent or not child or status.casefold() == "closed":
            continue
        open_rows.append((parent, child, status, archived, rollout_path))
        open_children[parent].add(child)
        if child in active_thread_ids:
            skipped_active.add(child)
            continue
        if archived is None:
            missing_threads += 1
            continue
        event = "archived" if bool(archived) else _terminal_rollout_event(
            rollout_path, codex_home=codex_home
        )
        if event:
            terminal[child] = event
        else:
            skipped_nonterminal += 1

    # A terminal parent with a live/non-terminal open child is not safe to
    # archive.  Remove unsafe ancestors until the set reaches a fixed point.
    safe = set(terminal)
    changed = True
    while changed:
        changed = False
        for node in tuple(safe):
            if any(child not in safe for child in open_children.get(node, ())):
                safe.remove(node)
                changed = True

    closed_edges = [
        {
            "parent_thread_id": parent,
            "child_thread_id": child,
            "previous_status": status,
        }
        for parent, child, status, _archived, _rollout in open_rows
        if child in safe
    ]
    event_counts: dict[str, int] = defaultdict(int)
    for child in safe:
        event_counts[terminal[child]] += 1
    return {
        "closed_edges": closed_edges,
        "candidate_threads": sorted(safe),
        "skipped_active": sorted(skipped_active),
        "missing_thread_count": missing_threads,
        "skipped_nonterminal_count": skipped_nonterminal,
        "terminal_event_counts": dict(sorted(event_counts.items())),
        "open_edge_count": len(open_rows),
    }


def _descendants(
    conn: sqlite3.Connection,
    root_thread_id: str,
    *,
    active_thread_ids: set[str],
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT parent_thread_id, child_thread_id, status "
        "FROM thread_spawn_edges"
    ).fetchall()
    by_parent: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for parent, child, status in rows:
        parent_id = _safe_id(parent)
        child_id = _safe_id(child)
        if not parent_id or not child_id:
            continue
        by_parent[parent_id].append((child_id, str(status or "")))

    seen = {root_thread_id}
    queue: deque[str] = deque([root_thread_id])
    closed_edges: list[dict[str, str]] = []
    candidate_threads: list[str] = []
    skipped_active: list[str] = []

    while queue:
        parent = queue.popleft()
        for child, status in by_parent.get(parent, []):
            if child in seen:
                continue
            seen.add(child)
            if child in active_thread_ids:
                skipped_active.append(child)
                # An active branch is a hard boundary.  It may have nested
                # descendants that are still running; do not touch them.
                continue
            if status.casefold() != "closed":
                closed_edges.append({
                    "parent_thread_id": parent,
                    "child_thread_id": child,
                    "previous_status": status,
                })
            candidate_threads.append(child)
            queue.append(child)
            if len(seen) > MAX_DESCENDANTS:
                raise _ReconcileError("descendant_limit_exceeded", status="bounded")

    return {
        "closed_edges": closed_edges,
        "candidate_threads": candidate_threads,
        "skipped_active": sorted(set(skipped_active)),
    }


def _columns_for_update(columns: set[str], *, now_sec: int, now_ms: int) -> tuple[str, list[Any]]:
    fields: list[str] = []
    values: list[Any] = []
    # These columns exist in current Codex state_5.sqlite.  Keeping optional
    # branches lets a read-only older fixture pass preflight safely.
    for name, value in (
        ("archived", 1),
        ("archived_at", now_sec),
        ("updated_at", now_sec),
        ("updated_at_ms", now_ms),
        ("recency_at", now_sec),
        ("recency_at_ms", now_ms),
    ):
        if name in columns:
            fields.append(f"{name}=?")
            values.append(value)
    return ", ".join(fields), values


def _reconcile_impl(
    *,
    root_thread_id: str,
    state_db_path: Path,
    codex_home: Path,
    backup_dir: Path,
    receipt_dir: Path,
    dry_run: bool,
    active_thread_ids: set[str],
    busy_timeout_ms: int,
    backup_retention: int,
) -> dict[str, Any]:
    result = _base_result(
        root_thread_id=root_thread_id, db_path=state_db_path, dry_run=dry_run
    )
    if not _is_within(state_db_path, codex_home):
        raise _ReconcileError("state_db_path_outside_codex_home", status="unsafe_path")
    if not _is_within(backup_dir, codex_home):
        raise _ReconcileError("backup_path_outside_codex_home", status="unsafe_path")
    if not state_db_path.exists():
        raise _ReconcileError("state_db_missing", status="missing")
    if not state_db_path.is_file():
        raise _ReconcileError("state_db_not_regular_file", status="unsafe_path")

    # ``mode=rw`` is essential: sqlite3.connect(path) would silently create a
    # missing DB.  The existence check above is still kept for a clear receipt.
    uri = f"file:{state_db_path.as_posix()}?mode=rw"
    try:
        conn = sqlite3.connect(
            uri,
            uri=True,
            timeout=max(0.001, min(int(busy_timeout_ms), 30_000) / 1000),
        )
    except sqlite3.OperationalError as exc:
        reason = "state_db_locked" if "locked" in str(exc).casefold() else "state_db_open_failed"
        raise _ReconcileError(reason) from exc

    backup_path = ""
    try:
        _configure_connection(conn, busy_timeout_ms)
        columns = _schema_preflight(conn)
        root_exists = conn.execute(
            "SELECT 1 FROM threads WHERE id=?", (root_thread_id,)
        ).fetchone()
        if root_exists is None:
            result.update({
                "ok": True,
                "status": "root_not_found",
                "reason": "root_thread_not_found",
                "active_thread_ids": sorted(active_thread_ids),
                "active_whitelist": sorted(active_thread_ids),
            })
            return result

        graph = _descendants(
            conn, root_thread_id, active_thread_ids=active_thread_ids
        )
        closed_edges = graph["closed_edges"]
        candidate_threads = graph["candidate_threads"]
        # Only archive rows that still exist; an orphan edge is reported and
        # closed, but no phantom thread row is ever inserted.
        thread_rows = (
            conn.execute(
                "SELECT id, archived FROM threads WHERE id IN ({})".format(
                    ",".join("?" for _ in candidate_threads) or "NULL"
                ),
                tuple(candidate_threads),
            ).fetchall()
            if candidate_threads
            else []
        )
        existing_threads = {str(row[0]) for row in thread_rows}
        unarchived_threads = {
            str(row[0]) for row in thread_rows if not bool(row[1])
        }
        missing_threads = sorted(set(candidate_threads) - existing_threads)

        result.update({
            "active_thread_ids": sorted(active_thread_ids),
            "active_whitelist": sorted(active_thread_ids),
            "closed_edge_ids": [item["child_thread_id"] for item in closed_edges],
            "closed_edges": closed_edges,
            "candidate_edge_ids": [item["child_thread_id"] for item in closed_edges],
            "candidate_thread_ids": list(candidate_threads),
            "skipped_active_thread_ids": graph["skipped_active"],
            "missing_thread_ids": missing_threads,
        })

        if dry_run:
            result.update({
                "ok": True,
                "status": "dry_run",
                "reason": (
                    "would_reconcile"
                    if (closed_edges or unarchived_threads)
                    else "already_reconciled"
                ),
                "changed": False,
                "closed_edge_count": len(closed_edges),
                "archived_thread_count": len(unarchived_threads),
            })
            return result

        now_ms = _now_ms()
        existing_backups = _backup_files(backup_dir)
        needs_change = bool(closed_edges or unarchived_threads)
        # Snapshot before every actual mutation.  If no mutation is needed,
        # make one daily snapshot so recovery remains available without
        # creating a backup for every Stop event.
        should_backup = needs_change or not _has_backup_today(existing_backups, now_ms)
        if should_backup:
            backup_path_obj, retained = _online_backup(
                conn,
                db_path=state_db_path,
                backup_dir=backup_dir,
                now_ms=now_ms,
                retention=backup_retention,
            )
            backup_path = str(backup_path_obj)
            result["backup_path"] = backup_path
            result["restore_path"] = backup_path
            result["backup_paths"] = [str(item) for item in retained]
        else:
            result["backup_paths"] = [str(item) for item in existing_backups]
            if existing_backups:
                result["restore_path"] = str(existing_backups[0])

        if not needs_change:
            result.update({
                "ok": True,
                "status": "noop",
                "reason": "already_reconciled",
            })
            return result

        now_sec = now_ms // 1000
        conn.execute("BEGIN IMMEDIATE")
        try:
            closed: list[dict[str, str]] = []
            for item in closed_edges:
                cursor = conn.execute(
                    "UPDATE thread_spawn_edges SET status='closed' "
                    "WHERE parent_thread_id=? AND child_thread_id=? "
                    "AND status <> 'closed'",
                    (item["parent_thread_id"], item["child_thread_id"]),
                )
                if cursor.rowcount:
                    closed.append(item)

            archived: list[str] = []
            thread_update, thread_values = _columns_for_update(
                columns["threads"], now_sec=now_sec, now_ms=now_ms
            )
            if not thread_update:
                raise _ReconcileError("state_db_threads_not_writable", status="schema_mismatch")
            for child in sorted(unarchived_threads):
                cursor = conn.execute(
                    f"UPDATE threads SET {thread_update} WHERE id=? "
                    "AND id<>? AND archived=0",
                    (*thread_values, child, root_thread_id),
                )
                if cursor.rowcount:
                    archived.append(child)

            # A parent recency bump makes the Codex panel refresh immediately.
            recency_fields: list[str] = []
            recency_values: list[Any] = []
            for name, value in (
                ("updated_at", now_sec),
                ("updated_at_ms", now_ms),
                ("recency_at", now_sec),
                ("recency_at_ms", now_ms),
            ):
                if name in columns["threads"]:
                    recency_fields.append(f"{name}=?")
                    recency_values.append(value)
            if recency_fields and (closed or archived):
                conn.execute(
                    f"UPDATE threads SET {', '.join(recency_fields)} WHERE id=?",
                    (*recency_values, root_thread_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        result.update({
            "ok": True,
            "status": "reconciled" if (closed or archived) else "noop",
            "reason": "reconciled" if (closed or archived) else "already_reconciled",
            "changed": bool(closed or archived),
            "closed_edges": closed,
            "closed_edge_ids": [item["child_thread_id"] for item in closed],
            "archived_thread_ids": archived,
            "closed_edge_count": len(closed),
            "archived_thread_count": len(archived),
        })
        return result
    finally:
        conn.close()


def _reconcile_global_impl(
    *,
    state_db_path: Path,
    codex_home: Path,
    backup_dir: Path,
    dry_run: bool,
    active_thread_ids: set[str],
    busy_timeout_ms: int,
    backup_retention: int,
) -> dict[str, Any]:
    """Reconcile every explicitly terminal stale edge in the Codex index."""

    result = _base_result(
        root_thread_id="", db_path=state_db_path, dry_run=dry_run
    )
    result["global_reconcile"] = True
    if not _is_within(state_db_path, codex_home):
        raise _ReconcileError("state_db_path_outside_codex_home", status="unsafe_path")
    if not _is_within(backup_dir, codex_home):
        raise _ReconcileError("backup_path_outside_codex_home", status="unsafe_path")
    if not state_db_path.exists():
        raise _ReconcileError("state_db_missing", status="missing")
    if not state_db_path.is_file():
        raise _ReconcileError("state_db_not_regular_file", status="unsafe_path")

    uri = f"file:{state_db_path.as_posix()}?mode=rw"
    try:
        conn = sqlite3.connect(
            uri,
            uri=True,
            timeout=max(0.001, min(int(busy_timeout_ms), 30_000) / 1000),
        )
    except sqlite3.OperationalError as exc:
        reason = "state_db_locked" if "locked" in str(exc).casefold() else "state_db_open_failed"
        raise _ReconcileError(reason) from exc

    try:
        _configure_connection(conn, busy_timeout_ms)
        columns = _schema_preflight(conn)
        graph = _global_terminal_graph(
            conn,
            codex_home=codex_home,
            active_thread_ids=active_thread_ids,
        )
        closed_edges = graph["closed_edges"]
        candidate_threads = graph["candidate_threads"]
        thread_rows = (
            conn.execute(
                "SELECT id, archived FROM threads WHERE id IN ({})".format(
                    ",".join("?" for _ in candidate_threads) or "NULL"
                ),
                tuple(candidate_threads),
            ).fetchall()
            if candidate_threads
            else []
        )
        unarchived_threads = {
            str(row[0]) for row in thread_rows if not bool(row[1])
        }
        result.update({
            "active_thread_ids": sorted(active_thread_ids),
            "active_whitelist": sorted(active_thread_ids),
            "closed_edge_ids": [item["child_thread_id"] for item in closed_edges],
            "closed_edges": closed_edges,
            "candidate_edge_ids": [item["child_thread_id"] for item in closed_edges],
            "candidate_thread_ids": list(candidate_threads),
            "skipped_active_thread_ids": graph["skipped_active"],
            "missing_thread_count": graph["missing_thread_count"],
            "skipped_nonterminal_count": graph["skipped_nonterminal_count"],
            "terminal_event_counts": graph["terminal_event_counts"],
            "open_edge_count": graph["open_edge_count"],
        })
        if dry_run:
            result.update({
                "ok": True,
                "status": "dry_run",
                "reason": (
                    "would_reconcile"
                    if (closed_edges or unarchived_threads)
                    else "already_reconciled"
                ),
                "closed_edge_count": len(closed_edges),
                "archived_thread_count": len(unarchived_threads),
            })
            return result

        needs_change = bool(closed_edges or unarchived_threads)
        if not needs_change:
            result.update({
                "ok": True,
                "status": "noop",
                "reason": "already_reconciled",
            })
            return result

        now_ms = _now_ms()
        backup_path, retained = _online_backup(
            conn,
            db_path=state_db_path,
            backup_dir=backup_dir,
            now_ms=now_ms,
            retention=backup_retention,
        )
        result.update({
            "backup_path": str(backup_path),
            "restore_path": str(backup_path),
            "backup_paths": [str(item) for item in retained],
        })

        now_sec = now_ms // 1000
        conn.execute("BEGIN IMMEDIATE")
        try:
            closed: list[dict[str, str]] = []
            for item in closed_edges:
                cursor = conn.execute(
                    "UPDATE thread_spawn_edges SET status='closed' "
                    "WHERE parent_thread_id=? AND child_thread_id=? "
                    "AND status <> 'closed'",
                    (item["parent_thread_id"], item["child_thread_id"]),
                )
                if cursor.rowcount:
                    closed.append(item)

            archived: list[str] = []
            thread_update, thread_values = _columns_for_update(
                columns["threads"], now_sec=now_sec, now_ms=now_ms
            )
            if not thread_update:
                raise _ReconcileError(
                    "state_db_threads_not_writable", status="schema_mismatch"
                )
            for child in sorted(unarchived_threads):
                cursor = conn.execute(
                    f"UPDATE threads SET {thread_update} WHERE id=? AND archived=0",
                    (*thread_values, child),
                )
                if cursor.rowcount:
                    archived.append(child)

            parent_ids = sorted({item["parent_thread_id"] for item in closed})
            recency_fields: list[str] = []
            recency_values: list[Any] = []
            for name, value in (
                ("updated_at", now_sec),
                ("updated_at_ms", now_ms),
                ("recency_at", now_sec),
                ("recency_at_ms", now_ms),
            ):
                if name in columns["threads"]:
                    recency_fields.append(f"{name}=?")
                    recency_values.append(value)
            if recency_fields:
                for parent in parent_ids:
                    conn.execute(
                        f"UPDATE threads SET {', '.join(recency_fields)} WHERE id=?",
                        (*recency_values, parent),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        result.update({
            "ok": True,
            "status": "reconciled",
            "reason": "reconciled",
            "changed": bool(closed or archived),
            "closed_edge_count": len(closed),
            "archived_thread_count": len(archived),
            "closed_edge_ids": [item["child_thread_id"] for item in closed],
            "archived_thread_ids": archived,
        })
        return result
    finally:
        conn.close()


class CodexSubagentReconciler:
    """Reusable safe reconciler bound to one Codex home/database."""

    def __init__(
        self,
        state_db_path: str | Path | None = None,
        *,
        codex_home: str | Path | None = None,
        backup_dir: str | Path | None = None,
        receipt_dir: str | Path | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        backup_retention: int = DEFAULT_BACKUP_RETENTION,
    ) -> None:
        self.codex_home = resolve_codex_home(codex_home)
        self.state_db_path = resolve_state_db_path(
            state_db_path, codex_home=self.codex_home
        )
        self.backup_dir = (
            Path(backup_dir).expanduser().resolve(strict=False)
            if backup_dir is not None
            else self.codex_home / DEFAULT_BACKUP_DIR_NAME
        )
        self.receipt_dir = (
            Path(receipt_dir).expanduser().resolve(strict=False)
            if receipt_dir is not None
            else self.codex_home / DEFAULT_RECEIPT_DIR_NAME
        )
        self.busy_timeout_ms = busy_timeout_ms
        self.backup_retention = backup_retention

    def reconcile(
        self,
        root_thread_id: str | None = None,
        *,
        active_thread_ids: Iterable[Any] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        trusted_root = _safe_id(root_thread_id)
        result = _base_result(
            root_thread_id=trusted_root,
            db_path=self.state_db_path,
            dry_run=dry_run,
        )
        if not trusted_root:
            result.update({
                "status": "skipped",
                "reason": "trusted_codex_thread_id_missing",
                "ok": True,
            })
            result["diagnostic_receipt"] = _write_receipt(result, self.receipt_dir)
            return result

        active = _active_ids(active_thread_ids) | _read_active_ids_from_env()
        try:
            result = _reconcile_impl(
                root_thread_id=trusted_root,
                state_db_path=self.state_db_path,
                codex_home=self.codex_home,
                backup_dir=self.backup_dir,
                receipt_dir=self.receipt_dir,
                dry_run=dry_run,
                active_thread_ids=active,
                busy_timeout_ms=self.busy_timeout_ms,
                backup_retention=self.backup_retention,
            )
        except _ReconcileError as exc:
            result.update({
                "ok": False,
                "degraded": True,
                "status": exc.status,
                "reason": exc.reason,
                "active_thread_ids": sorted(active),
            })
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
                result.update({
                    "ok": False,
                    "degraded": True,
                    "status": "locked",
                    "reason": "state_db_locked",
                    "active_thread_ids": sorted(active),
                })
            else:
                result.update({
                    "ok": False,
                    "degraded": True,
                    "status": "degraded",
                    "reason": f"state_db_open_failed:{type(exc).__name__}",
                    "active_thread_ids": sorted(active),
                })
        except sqlite3.DatabaseError as exc:
            result.update({
                "ok": False,
                "degraded": True,
                "status": "corrupt",
                "reason": f"state_db_error:{type(exc).__name__}",
                "active_thread_ids": sorted(active),
            })
        except (OSError, ValueError) as exc:
            result.update({
                "ok": False,
                "degraded": True,
                "status": "degraded",
                "reason": f"reconcile_failed:{type(exc).__name__}",
                "active_thread_ids": sorted(active),
            })
        except Exception as exc:  # host hook must remain best-effort
            result.update({
                "ok": False,
                "degraded": True,
                "status": "degraded",
                "reason": f"reconcile_failed:{type(exc).__name__}",
                "active_thread_ids": sorted(active),
            })

        result["diagnostic_receipt"] = _write_receipt(result, self.receipt_dir)
        return _json_safe(result)

    def dry_run(
        self,
        root_thread_id: str | None = None,
        *,
        active_thread_ids: Iterable[Any] | None = None,
    ) -> dict[str, Any]:
        return self.reconcile(
            root_thread_id,
            active_thread_ids=active_thread_ids,
            dry_run=True,
        )

    def reconcile_global(
        self,
        *,
        active_thread_ids: Iterable[Any] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Repair globally stale edges backed by explicit terminal events."""

        active = _active_ids(active_thread_ids) | _read_active_ids_from_env()
        result = _base_result(
            root_thread_id="", db_path=self.state_db_path, dry_run=dry_run
        )
        result["global_reconcile"] = True
        try:
            result = _reconcile_global_impl(
                state_db_path=self.state_db_path,
                codex_home=self.codex_home,
                backup_dir=self.backup_dir,
                dry_run=dry_run,
                active_thread_ids=active,
                busy_timeout_ms=self.busy_timeout_ms,
                backup_retention=self.backup_retention,
            )
        except _ReconcileError as exc:
            result.update({
                "ok": False,
                "degraded": True,
                "status": exc.status,
                "reason": exc.reason,
                "active_thread_ids": sorted(active),
            })
        except sqlite3.OperationalError as exc:
            locked = "locked" in str(exc).casefold() or "busy" in str(exc).casefold()
            result.update({
                "ok": False,
                "degraded": True,
                "status": "locked" if locked else "degraded",
                "reason": "state_db_locked" if locked else f"state_db_open_failed:{type(exc).__name__}",
                "active_thread_ids": sorted(active),
            })
        except sqlite3.DatabaseError as exc:
            result.update({
                "ok": False,
                "degraded": True,
                "status": "corrupt",
                "reason": f"state_db_error:{type(exc).__name__}",
                "active_thread_ids": sorted(active),
            })
        except (OSError, ValueError) as exc:
            result.update({
                "ok": False,
                "degraded": True,
                "status": "degraded",
                "reason": f"reconcile_failed:{type(exc).__name__}",
                "active_thread_ids": sorted(active),
            })
        except Exception as exc:  # host hook must remain best-effort
            result.update({
                "ok": False,
                "degraded": True,
                "status": "degraded",
                "reason": f"reconcile_failed:{type(exc).__name__}",
                "active_thread_ids": sorted(active),
            })
        result["diagnostic_receipt"] = _write_receipt(result, self.receipt_dir)
        return _json_safe(result)


def reconcile_codex_subagents(
    root_thread_id: str | None = None,
    *,
    state_db_path: str | Path | None = None,
    codex_home: str | Path | None = None,
    backup_dir: str | Path | None = None,
    receipt_dir: str | Path | None = None,
    active_thread_ids: Iterable[Any] | None = None,
    dry_run: bool = False,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    backup_retention: int = DEFAULT_BACKUP_RETENTION,
) -> dict[str, Any]:
    """Reconcile one trusted root and return a JSON-safe result dictionary."""

    return CodexSubagentReconciler(
        state_db_path,
        codex_home=codex_home,
        backup_dir=backup_dir,
        receipt_dir=receipt_dir,
        busy_timeout_ms=busy_timeout_ms,
        backup_retention=backup_retention,
    ).reconcile(
        root_thread_id,
        active_thread_ids=active_thread_ids,
        dry_run=dry_run,
    )


def dry_run_codex_subagents(
    root_thread_id: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    kwargs["dry_run"] = True
    return reconcile_codex_subagents(root_thread_id, **kwargs)


def reconcile_codex_subagents_json(
    root_thread_id: str | None = None,
    **kwargs: Any,
) -> str:
    """CLI/integration-friendly JSON representation of reconcile()."""

    return json.dumps(
        reconcile_codex_subagents(root_thread_id, **kwargs),
        ensure_ascii=True,
        sort_keys=True,
    )


def dry_run_codex_subagents_json(
    root_thread_id: str | None = None,
    **kwargs: Any,
) -> str:
    kwargs["dry_run"] = True
    return reconcile_codex_subagents_json(root_thread_id, **kwargs)


def reconcile_global_codex_subagents(
    *,
    state_db_path: str | Path | None = None,
    codex_home: str | Path | None = None,
    backup_dir: str | Path | None = None,
    receipt_dir: str | Path | None = None,
    active_thread_ids: Iterable[Any] | None = None,
    dry_run: bool = False,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    backup_retention: int = DEFAULT_BACKUP_RETENTION,
) -> dict[str, Any]:
    """Globally reconcile only edges with explicit terminal rollout proof."""

    return CodexSubagentReconciler(
        state_db_path,
        codex_home=codex_home,
        backup_dir=backup_dir,
        receipt_dir=receipt_dir,
        busy_timeout_ms=busy_timeout_ms,
        backup_retention=backup_retention,
    ).reconcile_global(active_thread_ids=active_thread_ids, dry_run=dry_run)


def dry_run_global_codex_subagents(**kwargs: Any) -> dict[str, Any]:
    kwargs["dry_run"] = True
    return reconcile_global_codex_subagents(**kwargs)


# Short aliases are useful to host integrations and keep the public surface
# discoverable without tying callers to a class implementation.
reconcile = reconcile_codex_subagents
dry_run = dry_run_codex_subagents


__all__ = [
    "CodexSubagentReconciler",
    "RECONCILE_VERSION",
    "codex_thread_matches_workspace",
    "dry_run",
    "dry_run_codex_subagents",
    "dry_run_codex_subagents_json",
    "dry_run_global_codex_subagents",
    "reconcile",
    "reconcile_codex_subagents",
    "reconcile_codex_subagents_json",
    "reconcile_global_codex_subagents",
    "resolve_codex_home",
    "resolve_state_db_path",
    "sanitize_reconcile_result",
    "trusted_codex_thread_id",
]
