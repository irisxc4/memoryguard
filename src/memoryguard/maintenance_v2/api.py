"""Public Phase 7 API with sanitized receipts and explicit trusted ports."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .blob_sweep import BlobSweepExecutor, BlobSweepResult, SweepSafetyPort
from .adapters import ReadOnlyAdapterError, SQLiteReadOnlyAdapter, assert_lexical_safe
from .models import MaintenanceContext
from .reference_audit import Blocker, ReferenceAudit
from .registry import DEFAULT_REGISTRY, DomainRegistry
from .sqlite_maintenance import MaintenanceActionResult, SQLiteMaintenanceExecutor
from .storage_report import StorageReportError, StorageReporter
from .store import MaintenanceStore
from ..system.manifest import ManifestManager
from ..storage.layout import LayoutError, WorkspaceV2Layout


class MaintenanceApiError(RuntimeError):
    pass


def _manifest_value(manifest: Any, field: str, default: Any = None) -> Any:
    """Read a manifest field without exposing the record or its pointers."""

    if isinstance(manifest, dict):
        return manifest.get(field, default)
    return getattr(manifest, field, default)


def _overview_digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _overview_blocker(domain: str, code: str) -> dict[str, str]:
    return {"domain": str(domain), "code": str(code)}


def _immutable_readonly_candidate(path: Path) -> bool:
    """Use SQLite immutable mode only when no sidecar needs to be replayed.

    A normal ``mode=ro`` WAL open may create ``-shm``.  Immutable mode avoids
    that write for a clean main database; existing WAL/journal sidecars remain
    on the leased read-only path so their committed state is not ignored.
    """

    return not any(path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))


class MaintenanceV2Api:
    """Narrow service boundary; no mapping can manufacture maintenance trust."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        registry: DomainRegistry = DEFAULT_REGISTRY,
        maintenance_store: MaintenanceStore | None = None,
        manifest: ManifestManager | None = None,
        sweep_safety_port: SweepSafetyPort | None = None,
        quiescence_verifier: Any = None,
        outbox_verifier: Any = None,
    ) -> None:
        audit = ReferenceAudit(workspace, registry=registry)
        self.workspace = audit.workspace
        self.registry = registry
        self.store = maintenance_store
        self.manifest = manifest or ManifestManager(self.workspace)
        self.sweep_safety_port = sweep_safety_port
        self.quiescence_verifier = quiescence_verifier
        self.outbox_verifier = outbox_verifier

    def audit_references(self) -> dict[str, Any]:
        return ReferenceAudit(self.workspace, registry=self.registry).audit().to_public_dict()

    audit = audit_references

    def storage_report(self, domain: str) -> dict[str, Any]:
        if domain not in self.registry:
            raise MaintenanceApiError("unknown authoritative storage domain")
        spec = self.registry[domain]
        path = self.registry.path_for(self.workspace, domain)
        report = StorageReporter().report(path, domain=domain)
        payload = report.to_dict()
        payload["path"] = (Path(spec.relative_path) / spec.db_name).as_posix()
        return payload

    report = storage_report

    def get_storage_overview(self) -> dict[str, Any]:
        """Return a public, read-only summary of the V2 authoritative stores.

        This method intentionally does not call any domain Store or legacy
        filesystem aggregator.  Paths are checked against ``WorkspaceV2Layout``
        before opening, and only registry schema metadata plus aggregate
        counters are returned.  A malformed/future/partial store blocks the
        whole envelope while preserving safe summaries for domains that were
        already inspected.
        """

        blockers: list[dict[str, str]] = []
        domains: list[dict[str, Any]] = []
        try:
            layout = WorkspaceV2Layout(self.workspace)
            # ReferenceAudit performs the same lexical workspace check in the
            # constructor; keep this explicit so this service cannot drift to
            # a path-bearing legacy fallback.
            assert_lexical_safe(layout.workspace, layout.workspace)
        except (LayoutError, ReadOnlyAdapterError, OSError, ValueError):
            return {
                "status": "blocked",
                "blockers": [_overview_blocker("workspace", "unsafe_workspace")],
                "manifest": {"status": "UNKNOWN", "generation": None},
                "domains": [],
                "digest": _overview_digest({"status": "blocked", "blockers": [_overview_blocker("workspace", "unsafe_workspace")], "manifest": {"status": "UNKNOWN", "generation": None}, "domains": []}),
            }

        # Reading the manifest is deliberately first and uses its read-only
        # ``current`` API.  Missing V2 state is a blocker; it must never be
        # healed by constructing a Store or creating directories.
        manifest_status = "UNKNOWN"
        manifest_generation: int | None = None
        try:
            if isinstance(self.manifest, ManifestManager):
                record = self.manifest.current(immutable=_immutable_readonly_candidate(self.manifest.db_path))
            else:
                # Test/dedicated providers may expose only the narrow current
                # method; do not require them to know the SQLite transport.
                record = self.manifest.current()
            raw_state = _manifest_value(record, "state", "UNKNOWN")
            manifest_status = getattr(raw_state, "value", raw_state)
            manifest_status = str(manifest_status)
            raw_generation = _manifest_value(record, "generation", None)
            if type(raw_generation) is int and raw_generation >= 0:
                manifest_generation = raw_generation
            else:
                blockers.append(_overview_blocker("system", "manifest_generation_invalid"))
            if manifest_status not in {"V2_READY", "V2_ACTIVE"}:
                blockers.append(_overview_blocker("system", "manifest_not_ready"))
        except Exception:
            # Deliberately redact ManifestError text: it can contain pointers
            # and implementation paths.  The envelope remains deterministic.
            blockers.append(_overview_blocker("system", "manifest_unreadable"))

        schema_audit = ReferenceAudit(self.workspace, registry=self.registry)
        for spec in self.registry:
            domain = spec.name
            path = self.registry.path_for(self.workspace, domain)
            domain_blockers: list[Blocker] = []
            snapshot = None
            report = None
            try:
                # Registry owns the exact relative path; Layout owns the
                # containment boundary.  The skills domain is intentionally
                # accepted here even though older Layout versions do not list
                # it in DOMAIN_DB_NAMES.
                contained = layout.assert_contained(path)
                assert_lexical_safe(contained, layout.workspace)
                if not contained.is_file():
                    raise FileNotFoundError(contained)
                immutable = _immutable_readonly_candidate(contained)
                adapter = SQLiteReadOnlyAdapter(contained, spec, domain=domain, immutable=immutable)
                schema_audit._validate_schema(spec, adapter, domain_blockers)
                snapshot = adapter.schema()
                # StorageReporter performs the leased mode=ro physical report.
                # Its layout binding is used whenever the layout knows this
                # domain; otherwise containment was already checked above.
                if domain in WorkspaceV2Layout.DOMAIN_DB_NAMES:
                    report = StorageReporter(layout, source_workspace=layout.workspace, immutable=immutable).report(contained, domain=domain)
                elif domain in {"scenario", "profile"}:
                    report = StorageReporter(layout, source_workspace=layout.workspace, immutable=immutable).report(contained, domain="projection")
                else:
                    report = StorageReporter(immutable=immutable).report(contained, domain=domain)
            except FileNotFoundError:
                domain_blockers.append(Blocker("missing_database", domain, ""))
            except (ReadOnlyAdapterError, StorageReportError, sqlite3.Error, OSError, ValueError, LayoutError):
                domain_blockers.append(Blocker("storage_unreadable", domain, ""))
            except Exception:
                domain_blockers.append(Blocker("storage_unreadable", domain, ""))

            codes = sorted({item.code for item in domain_blockers})
            for code in codes:
                blockers.append(_overview_blocker(domain, code))
            if report is None:
                domains.append({
                    "domain": domain,
                    "status": "blocked",
                    "bytes": {"allocated": 0, "free": 0, "wal": 0, "shm": 0},
                    "schema_version": None,
                    "health": {"readable": False, "integrity_ok": False, "foreign_key_errors": 0},
                    "counters": {"tables": 0, "rows": 0},
                })
                continue
            row_counts = dict(report.row_counts)
            domains.append({
                "domain": domain,
                "status": "blocked" if codes else "ready",
                "bytes": {
                    "allocated": int(report.allocated_bytes),
                    "free": int(report.free_bytes),
                    "wal": int(report.wal_bytes),
                    "shm": int(report.shm_bytes),
                },
                "schema_version": int(snapshot.user_version) if snapshot is not None else None,
                "health": {
                    "readable": bool(report.readable),
                    "integrity_ok": bool(report.integrity_ok),
                    "foreign_key_errors": int(report.foreign_key_errors),
                },
                "counters": {"tables": len(row_counts), "rows": sum(int(value) for value in row_counts.values())},
            })

        blockers = sorted({(item["domain"], item["code"]): item for item in blockers}.values(), key=lambda item: (item["domain"], item["code"]))
        payload: dict[str, Any] = {
            "status": "ready" if not blockers else "blocked",
            "blockers": blockers,
            "manifest": {"status": manifest_status, "generation": manifest_generation},
            "domains": domains,
        }
        payload["digest"] = _overview_digest(payload)
        return payload

    storage_overview = get_storage_overview

    def sweep(self, request_key: str, context: MaintenanceContext, *, apply: bool = False) -> BlobSweepResult:
        if not isinstance(context, MaintenanceContext):
            raise MaintenanceApiError("trusted MaintenanceContext instance is required")
        store = self.store or MaintenanceStore(self.workspace)
        return BlobSweepExecutor(
            self.workspace,
            maintenance_store=store,
            manifest=self.manifest,
            safety_port=self.sweep_safety_port,
            registry=self.registry,
        ).execute(request_key, context, apply=apply)

    def compact(
        self,
        domain: str,
        context: MaintenanceContext,
        *,
        job_id: str,
        apply: bool = False,
    ) -> MaintenanceActionResult:
        if domain not in self.registry:
            raise MaintenanceApiError("unknown authoritative storage domain")
        if not isinstance(context, MaintenanceContext):
            raise MaintenanceApiError("trusted MaintenanceContext instance is required")
        store = self.store or MaintenanceStore(self.workspace)
        executor = SQLiteMaintenanceExecutor(
            self.workspace,
            maintenance_store=store,
            quiescence_verifier=self.quiescence_verifier,
            outbox_verifier=self.outbox_verifier,
        )
        return executor.deep_compact(
            self.registry.path_for(self.workspace, domain),
            domain=domain,
            context=context,
            job_id=job_id,
            expected_generation=context.expected_generation,
            apply=apply,
        )


MaintenanceApi = MaintenanceV2Api


def get_storage_overview(workspace: str | Path, *, registry: DomainRegistry = DEFAULT_REGISTRY) -> dict[str, Any]:
    """Functional convenience wrapper for the native V2 overview service."""

    return MaintenanceV2Api(workspace, registry=registry).get_storage_overview()


__all__ = ["MaintenanceApiError", "MaintenanceV2Api", "MaintenanceApi", "get_storage_overview"]
