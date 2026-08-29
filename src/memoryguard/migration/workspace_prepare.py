"""Safe entry point for building a V2 shadow workspace.

The module is intentionally a thin orchestration layer around
``V2MigrationCoordinator`` and ``V2MigrationValidator``.  It adds the
workspace-only safety envelope (dry-run, path preflight, governance lock,
online source backups and immutable source hashes) without introducing a
second migration implementation.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import time
from typing import Any, Iterator, Mapping
from uuid import uuid4

from ..storage.database import open_database
from ..storage.layout import WorkspaceV2Layout
from ..storage.transaction import transaction
from ..system.manifest import ManifestManager, ManifestState
from .framework import V1Reader
from .v2_coordinator import V2MigrationCoordinator
from .v2_validator import V2MigrationValidator


SCHEMA = "memoryguard-v2-workspace-prepare-1"
_LOCK_NAME = "migration-governance.lock"
_BACKUP_DIR = "migration-backups"


class WorkspacePrepareError(RuntimeError):
    """A workspace failed a fail-closed preparation precondition."""


class WorkspaceCASConflict(WorkspacePrepareError):
    """The caller's expected manifest generation is stale."""


class WorkspaceSourceDrift(WorkspacePrepareError):
    """A source changed after its immutable build checkpoint."""


def _select_prepare_migration_id(
    current: Any,
    requested: str | None,
    *,
    apply: bool,
) -> str:
    """Choose a migration batch ID without reopening historical batches.

    A ``V2_BUILDING`` manifest is the only resumable state, so it must retain
    its current migration ID.  ``V1_ACTIVE`` may still carry the ID of a
    failed/rolled-back batch for audit purposes; that historical ID is never a
    resume capability and must not be reused by a new shadow build.
    """

    requested_id = str(requested or "").strip()
    state = getattr(current, "state", None)
    if state is ManifestState.V2_BUILDING:
        active_id = str(getattr(current, "migration_id", "") or "").strip()
        if not active_id:
            raise WorkspacePrepareError("V2_BUILDING manifest is missing migration_id")
        if requested_id and requested_id != active_id:
            raise WorkspacePrepareError("existing V2_BUILDING batch has a different migration_id")
        return active_id
    if state is ManifestState.V1_ACTIVE:
        historical_id = str(getattr(current, "migration_id", "") or "").strip()
        if requested_id:
            if historical_id and requested_id == historical_id:
                raise WorkspacePrepareError("historical migration_id cannot be reused from V1_ACTIVE")
            return requested_id
        return "prepare-" + uuid4().hex if apply else "prepare-dry-run"
    raise WorkspacePrepareError("workspace is not in a preparable manifest state")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _nonempty_wal(path: Path) -> bool:
    """Whether an immutable read would omit committed WAL frames."""

    wal = path.with_name(path.name + "-wal")
    try:
        return wal.is_file() and wal.stat().st_size > 0
    except OSError:
        return True


def _assert_immutable_read_safe(path: Path) -> None:
    if _nonempty_wal(path):
        raise WorkspacePrepareError(f"immutable dry-run read blocked by non-empty WAL: {path}")


def _stable_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_path(value: str | Path) -> Path:
    """Resolve lexical path while rejecting symlink/reparse components."""

    candidate = Path(os.path.abspath(os.fspath(Path(value).expanduser())))
    chain = list(reversed(candidate.parents)) + [candidate]
    for component in chain:
        try:
            info = os.lstat(component)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise WorkspacePrepareError(f"cannot inspect path component: {component}") from exc
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x0400):
            raise WorkspacePrepareError(f"symlink/reparse path component rejected: {component}")
    return candidate


def _relative(path: Path, roots: tuple[Path, ...]) -> str:
    for root in roots:
        try:
            return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
        except ValueError:
            continue
    return path.name


def _source_inventory(
    workspace: Path,
    *,
    data_home: Path | None,
    source_workspace: Path | None = None,
    migration_id: str,
    immutable: bool = False,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Collect metadata and hashes through existing read-only inventory APIs."""

    layout = WorkspaceV2Layout(workspace)
    selected_source_workspace = source_workspace or workspace
    validator = V2MigrationValidator(
        layout,
        data_home=data_home,
        migration_id=migration_id,
        source_workspace=source_workspace,
    )
    inventory = validator.source_inventory()
    roots = (
        (selected_source_workspace, data_home)
        if data_home is not None
        else (selected_source_workspace,)
    )
    files: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    backup_root = (workspace / ".memoryguard" / _BACKUP_DIR).resolve(strict=False)
    for key, item in sorted(inventory.items()):
        raw_path = str(item.get("path") or "")
        path = Path(raw_path) if raw_path else None
        if path is None or not path.is_file():
            continue
        try:
            if path.resolve(strict=False).is_relative_to(backup_root):
                continue
        except AttributeError:
            if str(path.resolve(strict=False)).startswith(str(backup_root)):
                continue
        path = _safe_path(path)
        digest = str(item.get("sha256") or _sha256(path))
        files[f"validator:{key}"] = {
            "path": str(path),
            "relative_path": _relative(path, roots),
            "sha256": digest,
            "kind": "sqlite" if path.suffix.casefold() in {".db", ".sqlite", ".sqlite3"} else "file",
        }
        hashes[f"validator:{key}"] = digest

    # Include manifest-like V1 files and any legacy source that the framework
    # knows about.  ``scan`` is read-only and reports absent sources explicitly.
    # Exclude V2 target domains from the legacy scan on resume; otherwise a
    # newly-created ``.memoryguard/knowledge`` database could be mistaken for
    # a V1 source.
    reader = V1Reader(selected_source_workspace, data_home=data_home, v2_root=layout.root)
    # Dry-run inventory uses the immutable reader transport so SQLite never
    # opens/updates a WAL or SHM sidecar.  Apply keeps the established scan
    # behaviour because it already enters the explicit write envelope below.
    snapshot = reader.read_only(strict=False) if immutable else reader.scan(strict=False)
    if immutable:
        for item in snapshot.items:
            path = Path(item.path)
            if path.suffix.casefold() in {".db", ".sqlite", ".sqlite3"}:
                _assert_immutable_read_safe(path)
    backup_root = (workspace / ".memoryguard" / _BACKUP_DIR).resolve(strict=False)
    for item in snapshot.items:
        path = Path(item.path)
        if not item.exists or not path.is_file():
            continue
        # Migration evidence is not a V1 source.  A previous failed or
        # completed preparation can leave snapshots/backups under
        # .memoryguard/migration-backups; including them here makes a later
        # migration consume its own evidence as input (the ouroboros bug).
        try:
            if path.resolve(strict=False).is_relative_to(backup_root):
                continue
        except AttributeError:
            if str(path.resolve(strict=False)).startswith(str(backup_root)):
                continue
        try:
            relative_v2 = path.resolve(strict=False).relative_to(layout.root.resolve(strict=False))
            if relative_v2.parts and relative_v2.parts[0] in layout.DOMAINS:
                continue
        except ValueError:
            pass
        path = _safe_path(path)
        key = f"legacy:{item.domain}:{_relative(path, roots)}"
        digest = str(item.sha256 or _sha256(path))
        files[key] = {
            "path": str(path),
            "relative_path": _relative(path, roots),
            "sha256": digest,
            "kind": "sqlite" if path.suffix.casefold() in {".db", ".sqlite", ".sqlite3"} else "file",
        }
        hashes[key] = digest

    # Existing system manifest is backed up before the coordinator writes it,
    # but is excluded from post-build source-drift comparisons.
    manifest_path = layout.manifest_db
    if manifest_path.is_file():
        manifest_path = _safe_path(manifest_path)
        digest = _sha256(manifest_path)
        files["system_manifest"] = {
            "path": str(manifest_path),
            "relative_path": _relative(manifest_path, roots),
            "sha256": digest,
            "kind": "sqlite",
        }
        hashes["system_manifest"] = digest
    return files, hashes


def _online_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        if source.suffix.casefold() in {".db", ".sqlite", ".sqlite3"}:
            source_conn = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
            target_conn = sqlite3.connect(str(temporary))
            try:
                source_conn.backup(target_conn)
                target_conn.commit()
            finally:
                target_conn.close()
                source_conn.close()
        else:
            shutil.copyfile(source, temporary)
        # Open read/write for fsync; Windows rejects fsync on a read-only
        # descriptor even though the file itself is valid.
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _backup_sources(
    workspace: Path,
    migration_id: str,
    files: Mapping[str, Mapping[str, Any]],
) -> tuple[Path, list[dict[str, Any]]]:
    root = workspace / ".memoryguard" / _BACKUP_DIR / migration_id
    root.mkdir(parents=True, exist_ok=True)
    backups: list[dict[str, Any]] = []
    for key, metadata in sorted(files.items()):
        source = Path(str(metadata["path"]))
        if not source.is_file():
            continue
        filename = f"{hashlib.sha256(key.encode()).hexdigest()[:16]}-{source.name}.bak"
        target = root / filename
        expected = str(metadata.get("sha256") or "")
        if target.is_file():
            action = "reused"
        else:
            _online_backup(source, target)
            if source.suffix.casefold() not in {".db", ".sqlite", ".sqlite3"} and expected and _sha256(target) != expected:
                raise WorkspacePrepareError(f"backup digest mismatch: {key}")
            action = "created"
        backups.append({
            "key": key,
            "source": str(source),
            "backup": str(target),
            "sha256": expected,
            "action": action,
        })
    return root, backups


def _snapshot_roots(workspace: Path, data_home: Path | None, backup_root: Path) -> tuple[Path, Path | None]:
    root = backup_root / "source-snapshot"
    snapshot_workspace = root / "workspace"
    snapshot_data_home: Path | None = None
    if data_home is not None:
        try:
            relative = data_home.resolve(strict=False).relative_to(workspace.resolve(strict=False))
            snapshot_data_home = snapshot_workspace / relative
        except ValueError:
            snapshot_data_home = root / "data-home"
    return snapshot_workspace, snapshot_data_home


def _copy_snapshot_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        shutil.copyfile(source, temporary)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _materialize_frozen_sources(
    workspace: Path,
    data_home: Path | None,
    backup_root: Path,
    backups: list[dict[str, Any]],
) -> dict[str, Any]:
    """Materialize one immutable V1 source tree from coherent online backups."""

    snapshot_workspace, snapshot_data_home = _snapshot_roots(workspace, data_home, backup_root)
    entries: list[dict[str, Any]] = []
    destination_digests: dict[str, str] = {}
    for item in backups:
        if str(item.get("key") or "") == "system_manifest":
            continue
        source = _safe_path(str(item["source"]))
        backup = _safe_path(str(item["backup"]))
        try:
            relative = source.resolve(strict=False).relative_to(workspace.resolve(strict=False))
            destination = snapshot_workspace / relative
            root_kind = "workspace"
        except ValueError:
            if data_home is not None and snapshot_data_home is not None:
                try:
                    relative = source.resolve(strict=False).relative_to(data_home.resolve(strict=False))
                    destination = snapshot_data_home / relative
                    root_kind = "data_home"
                except ValueError:
                    destination = backup_root / "source-snapshot" / "external" / hashlib.sha256(str(source).encode()).hexdigest()[:16] / source.name
                    root_kind = "external"
            else:
                destination = backup_root / "source-snapshot" / "external" / hashlib.sha256(str(source).encode()).hexdigest()[:16] / source.name
                root_kind = "external"
        destination = _safe_path(destination)
        backup_digest = _sha256(backup)
        destination_key = str(destination)
        if destination.is_file():
            if _sha256(destination) != backup_digest:
                raise WorkspacePrepareError(f"immutable source snapshot conflict: {destination}")
            action = "reused"
        else:
            existing_digest = destination_digests.get(destination_key)
            if existing_digest is not None and existing_digest != backup_digest:
                raise WorkspacePrepareError(f"source snapshot destination collision: {destination}")
            _copy_snapshot_file(backup, destination)
            if _sha256(destination) != backup_digest:
                raise WorkspacePrepareError(f"source snapshot copy digest mismatch: {destination}")
            action = "created"
        destination_digests[destination_key] = backup_digest
        entries.append({
            "key": str(item.get("key") or ""),
            "source": str(source),
            "backup": str(backup),
            "snapshot": str(destination),
            "root_kind": root_kind,
            "sha256": backup_digest,
            "action": action,
        })
    return {
        "workspace": str(snapshot_workspace),
        "data_home": str(snapshot_data_home) if snapshot_data_home is not None else "NOT_CONFIGURED",
        "entries": entries,
        "digest": _stable_digest(sorted((item["snapshot"], item["sha256"]) for item in entries)),
    }


def _archive_preexisting_shadow(layout: WorkspaceV2Layout, backup_root: Path) -> list[dict[str, Any]]:
    """Archive stale V2 targets while leaving V1 source/control directories intact."""

    archive_root = backup_root / "preexisting-shadow"
    moved: list[dict[str, Any]] = []
    for domain in layout.DOMAINS:
        if domain == "system":
            continue
        source = layout.domain_dir(domain)
        if not source.exists():
            continue
        _safe_path(source)
        destination = archive_root / domain
        if destination.exists():
            raise WorkspacePrepareError(f"preexisting shadow archive already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        moved.append({"domain": domain, "source": str(source), "archive": str(destination)})
    # V2-only auxiliary stores are outside WorkspaceV2Layout's Phase-2 domain
    # list. Preserve them too, but never move the system manifest itself.
    for name in ("governance_v2", "skills"):
        source = layout.root / name
        if not source.exists():
            continue
        _safe_path(source)
        destination = archive_root / name
        if destination.exists():
            raise WorkspacePrepareError(f"preexisting shadow archive already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        moved.append({"domain": name, "source": str(source), "archive": str(destination)})
    system_dir = layout.system
    for name in ("maintenance.db", "maintenance.db-wal", "maintenance.db-shm", "maintenance.db-journal"):
        source = system_dir / name
        if not source.exists():
            continue
        destination = archive_root / "system" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        moved.append({"domain": "system-maintenance", "source": str(source), "archive": str(destination)})
    return moved


def _verify_live_source_snapshot(snapshot: Mapping[str, Any], backup_root: Path) -> dict[str, Any]:
    """Compare live V1 sources to the frozen tree using coherent SQLite backups."""

    changed: list[str] = []
    missing: list[str] = []
    checked = 0
    seen_sources: set[str] = set()
    verify_root = backup_root / "live-verify"
    for item in snapshot.get("entries", []) if isinstance(snapshot.get("entries"), list) else []:
        if not isinstance(item, Mapping):
            continue
        source = Path(str(item.get("source") or ""))
        frozen = Path(str(item.get("snapshot") or ""))
        source_key = str(source.resolve(strict=False))
        if source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        if not source.is_file() or not frozen.is_file():
            missing.append(source_key)
            continue
        checked += 1
        try:
            if source.suffix.casefold() in {".db", ".sqlite", ".sqlite3"}:
                verify = verify_root / (hashlib.sha256(source_key.encode()).hexdigest()[:16] + "-" + source.name)
                _online_backup(source, verify)
                current_digest = _sha256(verify)
                try:
                    verify.unlink()
                except FileNotFoundError:
                    pass
            else:
                current_digest = _sha256(source)
            if current_digest != _sha256(frozen):
                changed.append(source_key)
        except Exception:
            changed.append(source_key)
    try:
        verify_root.rmdir()
    except OSError:
        pass
    return {
        "status": "PASS" if not changed and not missing else "DRIFT",
        "checked": checked,
        "changed": sorted(changed),
        "missing": sorted(missing),
        "snapshot_digest": str(snapshot.get("digest") or ""),
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _governance_lock(workspace: Path, *, apply: bool) -> Iterator[bool]:
    path = workspace / ".memoryguard" / _LOCK_NAME
    if not apply:
        if path.exists():
            raise WorkspacePrepareError("governance lock is held")
        yield False
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise WorkspacePrepareError("governance lock is held") from exc
    try:
        os.write(fd, f"pid={os.getpid()}\n".encode())
        os.fsync(fd)
        os.close(fd)
        yield True
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _record_failure(manifest: ManifestManager, migration_id: str, error: str) -> None:
    """Keep BUILDING and persist immutable failure evidence."""

    current = manifest.current()
    if current.state is not ManifestState.V2_BUILDING:
        return
    entry = {
        "status": "FAILED",
        "migration_id": migration_id,
        "error": str(error),
    }
    try:
        manifest.record_checkpoint_attempt({"workspace_prepare_failure": entry}, migration_id=migration_id)
    except Exception:
        # A prior immutable failure checkpoint is already durable; do not
        # replace it during a retry.
        pass
    # Also update the manifest's error envelope without changing state or
    # generation.  The existing ledger row remains associated with the batch.
    try:
        with open_database(manifest.db_path) as conn:
            with transaction(conn):
                now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                conn.execute(
                    "UPDATE manifest SET last_error=?,errors_json=?,updated_at=? WHERE manifest_id='workspace' AND state='V2_BUILDING'",
                    (str(error), json.dumps({"workspace_prepare": str(error)}, ensure_ascii=False, sort_keys=True), now),
                )
                # The initial V1_ACTIVE -> V2_BUILDING transition owns one
                # ledger row for this batch.  Marking that row failed records
                # the durable error without an illegal same-state transition.
                conn.execute(
                    "UPDATE migration_ledger SET status='failed',error_json=?,completed_at=? WHERE migration_id=? AND to_state='V2_BUILDING'",
                    (json.dumps({"workspace_prepare": str(error)}, ensure_ascii=False, sort_keys=True), now, migration_id),
                )
    except Exception:
        pass


def _clear_transient_error(manifest: ManifestManager, migration_id: str) -> None:
    """Clear current error envelope after a successful resumable retry."""

    try:
        with open_database(manifest.db_path) as conn:
            with transaction(conn):
                conn.execute(
                    "UPDATE manifest SET last_error='',errors_json='{}',updated_at=? WHERE manifest_id='workspace' AND state='V2_BUILDING' AND migration_id=?",
                    (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), migration_id),
                )
    except Exception:
        pass


def _target_metadata(layout: WorkspaceV2Layout) -> dict[str, dict[str, Any]]:
    domains: dict[str, dict[str, Any]] = {}
    for domain, path in layout.iter_db_paths():
        rel = path.relative_to(layout.workspace).as_posix()
        domains[rel] = {"domain": domain, "exists": path.is_file(), "bytes": path.stat().st_size if path.is_file() else 0}
    return domains


def _base_report(
    *,
    workspace: Path,
    mode: str,
    migration_id: str,
    manifest_state: str,
    generation: int,
    files: Mapping[str, Mapping[str, Any]],
    hashes: Mapping[str, str],
    backups: list[dict[str, Any]],
    domains: Mapping[str, Any],
    checkpoints: Mapping[str, Any],
    validator: Mapping[str, Any],
    failures: list[dict[str, Any]],
    writes: bool,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "status": "DRY_RUN" if mode == "dry_run" else ("FAILED" if failures else "V2_BUILDING"),
        "ok": mode == "dry_run" or not failures,
        "plan": {
            "mode": mode,
            "workspace": str(workspace),
            "migration_id": migration_id,
            "manifest_generation": generation,
            "manifest_state": manifest_state,
            "writes_performed": writes,
            "backup_dir": str(workspace / ".memoryguard" / _BACKUP_DIR / migration_id) if writes else "",
            "source_count": len(files),
        },
        "backups": backups,
        "source_hashes": dict(sorted(hashes.items())),
        "domains": dict(domains),
        "checkpoints": dict(checkpoints),
        "validator": dict(validator),
        "readiness_eligible": False,
        "failures": failures,
        "gates": {
            "dry_run_zero_write": (not writes) if mode == "dry_run" else True,
            "safe_paths": not any(item.get("kind") == "unsafe_path" for item in failures),
            "manifest_not_active": manifest_state != ManifestState.V2_ACTIVE.value,
            "readiness_eligible_false": True,
        },
    }


def prepare_v2_workspace(
    workspace: str | Path,
    *,
    apply: bool = False,
    data_home: str | Path | None = None,
    source_workspace: str | Path | None = None,
    migration_id: str | None = None,
    expected_generation: int | None = None,
    fail_at: str | None = None,
) -> dict[str, Any]:
    """Plan or apply one resumable V2 shadow build.

    ``apply=False`` performs no filesystem writes.  ``apply=True`` is the
    only mode that creates the V2 layout, backups, lock, manifest checkpoint,
    and migration databases.
    """

    workspace_path = _safe_path(workspace)
    data_path = _safe_path(data_home) if data_home is not None else None
    source_workspace_path = _safe_path(source_workspace) if source_workspace is not None else workspace_path
    layout = WorkspaceV2Layout(workspace_path)
    manifest = ManifestManager(layout)
    if not apply:
        _assert_immutable_read_safe(manifest.db_path)
    # A dry-run must not let SQLite create/update a manifest ``-shm`` file.
    # Apply intentionally retains the normal read path before its write lock.
    current = manifest.current(immutable=not apply)
    if current.state is ManifestState.V2_ACTIVE:
        raise WorkspacePrepareError("V2_ACTIVE workspace cannot be prepared")
    if current.state is ManifestState.V2_READY:
        raise WorkspacePrepareError("V2_READY workspace requires explicit activation workflow")
    observed_generation = current.generation
    effective_id = _select_prepare_migration_id(current, migration_id, apply=apply)
    files, hashes = _source_inventory(
        workspace_path,
        data_home=data_path,
        source_workspace=(source_workspace_path if source_workspace is not None else None),
        migration_id=effective_id,
        immutable=not apply,
    )
    if expected_generation is not None and int(expected_generation) != int(current.generation):
        raise WorkspaceCASConflict(f"manifest generation conflict: expected {expected_generation}, current {current.generation}")

    failures: list[dict[str, Any]] = []
    backups: list[dict[str, Any]] = []
    domains = _target_metadata(layout)
    checkpoints = dict(current.checkpoints)
    validator_payload: dict[str, Any] = {}

    if not apply:
        validator = V2MigrationValidator(
            layout,
            data_home=data_path,
            migration_id=effective_id,
            source_workspace=(source_workspace_path if source_workspace is not None else None),
        )
        validation = validator.validate(migration_id=effective_id)
        validator_payload = validation.to_dict()
        report = _base_report(
            workspace=workspace_path,
            mode="dry_run",
            migration_id=effective_id,
            manifest_state=current.state.value,
            generation=current.generation,
            files=files,
            hashes=hashes,
            backups=backups,
            domains=domains,
            checkpoints=checkpoints,
            validator=validator_payload,
            failures=failures,
            writes=False,
        )
        report["ok"] = True
        return report

    with _governance_lock(workspace_path, apply=True):
        # Re-read generation/state after acquiring the lock (CAS boundary).
        current = manifest.current()
        if current.state is ManifestState.V2_ACTIVE:
            raise WorkspacePrepareError("V2_ACTIVE workspace cannot be prepared")
        if current.state is ManifestState.V2_READY:
            raise WorkspacePrepareError("V2_READY workspace requires explicit activation workflow")
        if expected_generation is not None and int(expected_generation) != int(current.generation):
            raise WorkspaceCASConflict(f"manifest generation conflict: expected {expected_generation}, current {current.generation}")
        if current.generation != observed_generation:
            raise WorkspaceCASConflict(
                f"manifest generation changed before governance lock: observed {observed_generation}, current {current.generation}"
            )
        if current.state is ManifestState.V2_BUILDING:
            effective_id = _select_prepare_migration_id(current, migration_id, apply=True)
        elif migration_id:
            effective_id = _select_prepare_migration_id(current, migration_id, apply=True)
        backup_root, backups = _backup_sources(workspace_path, effective_id, files)
        snapshot = _materialize_frozen_sources(
            source_workspace_path, data_path, backup_root, backups,
        )
        archived_shadow: list[dict[str, Any]] = []
        if current.state is ManifestState.V1_ACTIVE:
            archived_shadow = _archive_preexisting_shadow(layout, backup_root)
        plan_payload = {
            "schema": SCHEMA,
            "migration_id": effective_id,
            "live_inventory_hashes": dict(sorted(hashes.items())),
            "backups": backups,
            "source_snapshot": snapshot,
            "archived_shadow": archived_shadow,
            "created_at": "",
        }
        _write_json(backup_root / "prepare-plan.json", plan_payload)

        live_verification: dict[str, Any] = {}
        try:
            snapshot_data = str(snapshot.get("data_home") or "")
            coordinator = V2MigrationCoordinator(
                workspace_path,
                data_home=data_path,
                source_workspace=str(snapshot["workspace"]),
                source_data_home=(None if snapshot_data in {"", "NOT_CONFIGURED"} else snapshot_data),
                migration_id=effective_id,
                fail_at=fail_at,
                keep_building_on_failure=True,
                expected_generation=current.generation,
            )
            result = coordinator.run(strict=False)
            checkpoints = result.checkpoints
            validator_payload = result.validation
            domains = {**domains, **result.domains}
            if result.errors or result.status == "FAILED":
                error = "; ".join(result.errors) or "phase2 migration failed"
                _record_failure(manifest, effective_id, error)
                failures.append({"kind": "migration", "message": error})
            else:
                _clear_transient_error(manifest, effective_id)
                # Phase 8 proved readiness requires the two V2 auxiliary
                # control domains that are intentionally outside Phase-1/2's
                # fixed core database list. Production prepare must initialize
                # the same stores instead of leaving that step fixture-only.
                try:
                    from ..assets_v2.store import ASSET_SCHEMA_VERSION, AssetStore
                    from ..codegraph_v2.store import CodeGraphStore
                    from ..maintenance_v2.store import MaintenanceStore
                    from ..projection_v2.store import ProjectionStore
                    from ..runtime_v2.working_memory import RUNTIME_V2_SCHEMA_VERSION, RuntimeStore
                    from ..skills_v2.store import SkillStore

                    runtime = RuntimeStore(workspace_path)
                    assets = AssetStore(workspace_path)
                    codegraph = CodeGraphStore(workspace_path)
                    projection = ProjectionStore(workspace_path)
                    skills = SkillStore(workspace_path)
                    maintenance = MaintenanceStore(workspace_path)
                    auxiliary = {
                        "status": "READY",
                        "runtime": {"schema_version": int(RUNTIME_V2_SCHEMA_VERSION)},
                        "assets": {"schema_version": int(ASSET_SCHEMA_VERSION)},
                        "codegraph": {"schema_version": int(codegraph.SCHEMA_VERSION)},
                        "projection": {"schema_version": int(projection.SCHEMA_VERSION)},
                        "skills": {
                            "schema_version": int(skills.SCHEMA_VERSION),
                            "schema_marker": str(skills.SCHEMA_MARKER),
                        },
                        "maintenance": {
                            "schema_version": int(maintenance.schema_version),
                            "schema_marker": str(maintenance.schema_marker),
                        },
                    }
                    manifest.record_checkpoint_attempt(
                        {"v2_auxiliary_initialized": auxiliary},
                        migration_id=effective_id,
                        expected_generation=manifest.current().generation,
                    )
                except Exception as aux_exc:
                    error = f"V2 auxiliary domain initialization failed: {type(aux_exc).__name__}"
                    _record_failure(manifest, effective_id, error)
                    failures.append({"kind": "auxiliary", "message": error})
            # Migration and validation above read only the immutable snapshot.
            # A second coherent online backup proves whether live V1 moved
            # while that shadow was being built. Drift blocks readiness but
            # never rewrites or invalidates the snapshot evidence itself.
            live_verification = _verify_live_source_snapshot(snapshot, backup_root)
            try:
                manifest.record_checkpoint_attempt(
                    {"phase2_live_source_verification": live_verification},
                    migration_id=effective_id,
                    expected_generation=manifest.current().generation,
                )
            except Exception as checkpoint_exc:
                error = f"live source verification checkpoint failed: {type(checkpoint_exc).__name__}"
                _record_failure(manifest, effective_id, error)
                failures.append({"kind": "source_verification", "message": error})
            if live_verification.get("status") != "PASS":
                error = "live V1 source drifted from frozen Phase-2 snapshot"
                _record_failure(manifest, effective_id, error)
                failures.append({"kind": "source_drift", "message": error, "details": live_verification})
        except Exception as exc:  # noqa: BLE001 - stable machine report
            error = f"{type(exc).__name__}: {exc}"
            try:
                _record_failure(manifest, effective_id, error)
            except Exception:
                pass
            failures.append({"kind": "apply", "message": error})
        current = manifest.current()
        checkpoints = dict(current.checkpoints)
        domains = {**domains, **_target_metadata(layout)}
        final_hashes = dict(getattr(locals().get("result", None), "source_hashes", {}) or hashes)
        report = _base_report(
            workspace=workspace_path,
            mode="apply",
            migration_id=effective_id,
            manifest_state=current.state.value,
            generation=current.generation,
            files=files,
            hashes=final_hashes,
            backups=backups,
            domains=domains,
            checkpoints=checkpoints,
            validator=validator_payload,
            failures=failures,
            writes=True,
        )
        report["source_snapshot"] = snapshot
        report["archived_shadow"] = archived_shadow
        report["live_source_verification"] = live_verification
        report["ok"] = not failures and current.state is ManifestState.V2_BUILDING
        return report


def verify_v2_source_snapshot(
    workspace: str | Path,
    *,
    data_home: str | Path | None = None,
    migration_id: str | None = None,
) -> dict[str, Any]:
    """Re-verify live V1 against the frozen Phase-2 source set.

    This is the activation-time drift gate.  It does not modify V1/V2 data or
    manifest state; the only temporary writes are coherent SQLite backup files
    under the existing migration-backup directory, removed before return.
    """

    workspace_path = _safe_path(workspace)
    data_path = _safe_path(data_home) if data_home is not None else None
    current = ManifestManager(WorkspaceV2Layout(workspace_path)).current()
    effective_id = str(migration_id or current.migration_id or "")
    if not effective_id:
        raise WorkspacePrepareError("V2 source snapshot migration_id is missing")
    backup_root = workspace_path / ".memoryguard" / _BACKUP_DIR / effective_id
    plan_path = backup_root / "prepare-plan.json"
    if not plan_path.is_file():
        raise WorkspacePrepareError("V2 source snapshot prepare plan is missing")
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspacePrepareError("V2 source snapshot prepare plan is unreadable") from exc
    if not isinstance(plan, Mapping) or str(plan.get("migration_id") or "") != effective_id:
        raise WorkspacePrepareError("V2 source snapshot prepare plan does not match migration_id")
    snapshot = plan.get("source_snapshot")
    if not isinstance(snapshot, Mapping):
        raise WorkspacePrepareError("V2 source snapshot metadata is missing")
    allowed = (backup_root / "source-snapshot").resolve(strict=False)
    try:
        Path(str(snapshot.get("workspace") or "")).resolve(strict=False).relative_to(allowed)
        raw_data = str(snapshot.get("data_home") or "")
        if raw_data not in {"", "NOT_CONFIGURED"}:
            Path(raw_data).resolve(strict=False).relative_to(allowed)
    except ValueError as exc:
        raise WorkspacePrepareError("V2 source snapshot path escapes migration backup root") from exc
    result = _verify_live_source_snapshot(snapshot, backup_root)
    result.update({
        "migration_id": effective_id,
        "workspace": str(workspace_path),
        "data_home_configured": data_path is not None,
        "activation_safe": result.get("status") == "PASS",
    })
    return result


__all__ = [
    "SCHEMA", "WorkspaceCASConflict", "WorkspacePrepareError",
    "WorkspaceSourceDrift", "prepare_v2_workspace", "verify_v2_source_snapshot",
]
