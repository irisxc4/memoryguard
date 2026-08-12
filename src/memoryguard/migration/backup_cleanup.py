"""Fail-closed cleanup for completed migration backup batches.

Migration backups are recovery evidence while a batch is building, ready, or
failed.  This module deliberately accepts one migration ID at a time: a
successful activation can remove only its own batch, never the whole backup
root or an arbitrary path supplied by a caller.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any


SCHEMA = "memoryguard-migration-backup-cleanup-1"
BACKUP_DIR = "migration-backups"
_MIGRATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_REPARSE_POINT = 0x0400


def _workspace_path(workspace: str | Path) -> Path:
    """Return an absolute workspace path and reject reparse components."""

    candidate = Path(os.path.abspath(os.fspath(Path(workspace).expanduser())))
    _assert_no_reparse_components(candidate)
    return candidate


def _assert_no_reparse_components(path: Path) -> None:
    """Reject symlink/reparse components before any cleanup is attempted."""

    chain = list(reversed(path.parents)) + [path]
    for component in chain:
        try:
            info = os.lstat(component)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise OSError(f"cannot inspect cleanup path component: {component}") from exc
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT):
            raise ValueError(f"symlink/reparse cleanup path component rejected: {component}")


def _backup_root(workspace: Path) -> Path:
    root = workspace / ".memoryguard" / BACKUP_DIR
    _assert_no_reparse_components(root)
    return root


def _migration_target(root: Path, migration_id: str) -> Path:
    value = str(migration_id or "").strip()
    if not _MIGRATION_ID.fullmatch(value):
        raise ValueError("migration_id must be one safe path component")
    target = root / value
    # The lexical parent check prevents a future change to the ID grammar from
    # turning this function into a recursive delete of the backup root.
    if target.parent != root:
        raise ValueError("migration_id must identify a direct backup child")
    try:
        target.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise ValueError("migration backup path escapes backup root") from exc
    if os.path.lexists(target):
        _assert_no_reparse_components(target)
    return target


def _relative_to_root(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _record_error(errors: list[dict[str, str]], path: Path, root: Path, exc: BaseException) -> None:
    errors.append(
        {
            "path": _relative_to_root(path, root),
            "error": f"{type(exc).__name__}: {exc}",
        }
    )


def _remove_entry(path: Path, root: Path, errors: list[dict[str, str]]) -> None:
    """Remove one entry without following symlink/reparse children."""

    try:
        path.relative_to(root)
    except ValueError as exc:
        _record_error(errors, path, root, ValueError("cleanup path escaped backup root"))
        return

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        _record_error(errors, path, root, exc)
        return

    if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT):
        # A child link itself is inside the allowed root.  Removing the link
        # cannot remove its target, and avoids ever traversing outside.
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            _record_error(errors, path, root, exc)
        return

    if stat.S_ISDIR(info.st_mode):
        try:
            children = [Path(item.path) for item in os.scandir(path)]
        except OSError as exc:
            _record_error(errors, path, root, exc)
            return
        for child in children:
            _remove_entry(child, root, errors)
        try:
            path.rmdir()
        except FileNotFoundError:
            pass
        except OSError as exc:
            _record_error(errors, path, root, exc)
        return

    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _record_error(errors, path, root, exc)


def _remaining(path: Path, root: Path) -> list[str]:
    """List residual entries without following symlinks."""

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return []
    except OSError:
        return [_relative_to_root(path, root)]

    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return [_relative_to_root(path, root)]

    found: list[str] = []
    try:
        children = [Path(item.path) for item in os.scandir(path)]
    except OSError:
        return [_relative_to_root(path, root)]
    for child in children:
        found.extend(_remaining(child, root))
    return found or [_relative_to_root(path, root)]


def _result(
    *,
    workspace: Path,
    migration_id: str,
    backup_root: Path,
    status: str,
    removed: bool,
    remaining: list[str],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    warning = bool(errors or remaining)
    return {
        "schema": SCHEMA,
        "status": "WARNING" if warning else status,
        "ok": not warning,
        "cleanup_warning": warning,
        "workspace": str(workspace),
        "backup_root": str(backup_root),
        "migration_id": migration_id,
        "removed": bool(removed),
        "remaining": sorted(set(remaining)),
        "errors": errors,
    }


def cleanup_migration_backups(
    workspace: str | Path,
    migration_id: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Remove one completed migration batch, reporting partial cleanup safely.

    ``dry_run`` is used by the active preview path.  It validates containment
    and reports what would remain without writing.  The function never raises
    for an expected filesystem cleanup failure; it returns ``WARNING`` with
    ``remaining`` so callers can keep a successful activation successful while
    surfacing cleanup debt.
    """

    raw_id = str(migration_id or "").strip()
    try:
        workspace_path = _workspace_path(workspace)
        backup_root = _backup_root(workspace_path)
        target = _migration_target(backup_root, raw_id)
    except Exception as exc:  # path safety is a reportable cleanup warning
        return {
            "schema": SCHEMA,
            "status": "WARNING",
            "ok": False,
            "cleanup_warning": True,
            "workspace": str(workspace),
            "backup_root": str(Path(workspace).expanduser() / ".memoryguard" / BACKUP_DIR),
            "migration_id": raw_id,
            "removed": False,
            "remaining": [raw_id] if raw_id else [],
            "errors": [{"path": raw_id or "<missing>", "error": f"{type(exc).__name__}: {exc}"}],
        }

    if not os.path.lexists(target):
        return _result(
            workspace=workspace_path,
            migration_id=raw_id,
            backup_root=backup_root,
            status="NOOP",
            removed=False,
            remaining=[],
            errors=[],
        )

    if dry_run:
        return _result(
            workspace=workspace_path,
            migration_id=raw_id,
            backup_root=backup_root,
            status="PENDING",
            removed=False,
            remaining=_remaining(target, backup_root),
            errors=[],
        )

    errors: list[dict[str, str]] = []
    _remove_entry(target, backup_root, errors)
    remaining = _remaining(target, backup_root)
    return _result(
        workspace=workspace_path,
        migration_id=raw_id,
        backup_root=backup_root,
        status="CLEANED",
        removed=not remaining,
        remaining=remaining,
        errors=errors,
    )


def cleanup_completed_recovery_materials(
    workspace: str | Path,
    migration_id: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Explicitly clean a completed batch, including phase recovery material.

    Recovery directories such as ``phase6-recovery`` are intentionally not
    special-cased: selecting a completed migration batch removes all of its
    contents under the same path-safe root.
    """

    return cleanup_migration_backups(workspace, migration_id, dry_run=dry_run)


__all__ = [
    "BACKUP_DIR",
    "SCHEMA",
    "cleanup_completed_recovery_materials",
    "cleanup_migration_backups",
]
