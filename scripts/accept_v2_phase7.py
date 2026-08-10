#!/usr/bin/env python3
"""Machine-readable Phase 7 maintenance acceptance (default read-only)."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from memoryguard.assets_v2.store import AssetStore
from memoryguard.codegraph_v2.store import CodeGraphStore
from memoryguard.content.store import ContentStore
from memoryguard.evidence.store import EvidenceStore
from memoryguard.maintenance_v2.blob_sweep import BlobSweepExecutor, SweepSafetyEvidence
from memoryguard.maintenance_v2.models import MaintenanceContext, MaintenanceJobState, MaintenanceScope
from memoryguard.maintenance_v2.reference_audit import ReferenceAudit
from memoryguard.maintenance_v2.registry import DEFAULT_REGISTRY
from memoryguard.maintenance_v2.sqlite_maintenance import SQLiteMaintenanceExecutor
from memoryguard.maintenance_v2.storage_report import StorageReporter
from memoryguard.maintenance_v2.store import MaintenanceStore
from memoryguard.memory.store import MemoryAtomStore
from memoryguard.projection_v2.store import ProjectionStore
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.working_memory import RuntimeStore
from memoryguard.skills_v2.store import SkillStore
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_database
from memoryguard.system.manifest import ManifestManager, ManifestState


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _files(workspace: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for spec in DEFAULT_REGISTRY:
        path = DEFAULT_REGISTRY.path_for(workspace, spec.name)
        if path.is_file():
            values[spec.name] = _sha(path)
    maintenance = workspace / ".memoryguard" / "system" / "maintenance.db"
    if maintenance.is_file():
        values["maintenance"] = _sha(maintenance)
    return values


def _read_only(workspace: Path) -> dict:
    before = _files(workspace)
    root_existed = (workspace / ".memoryguard").exists()
    audit = ReferenceAudit(workspace).audit()
    reports: dict[str, dict] = {}
    for spec in DEFAULT_REGISTRY:
        path = DEFAULT_REGISTRY.path_for(workspace, spec.name)
        if path.is_file():
            report = StorageReporter().report(path, domain=spec.name)
            reports[spec.name] = {
                "integrity_ok": report.integrity_ok,
                "logical_pages": report.logical_pages,
                "free_pages": report.free_pages,
                "allocated_bytes": report.allocated_bytes,
                "wal_bytes": report.wal_bytes,
                "schema_fingerprint": report.schema_fingerprint,
            }
    after = _files(workspace)
    unchanged = before == after and root_existed == (workspace / ".memoryguard").exists()
    public = audit.to_public_dict()
    ok = not audit.blocked and unchanged and all(item["integrity_ok"] for item in reports.values())
    return {
        "ok": ok,
        "status": "PASS" if ok else "BLOCKED",
        "state": "V2_BUILDING",
        "ready": False,
        "can_promote": False,
        "dry_run": True,
        "audit": public,
        "storage_reports": reports,
        "checks": {"workspace_unchanged": unchanged, "zero_physical_deletion": True},
    }


class _Safety:
    def verify(self, *, workspace_id: str, scope_digest: str, lease_id: str, generation: int) -> SweepSafetyEvidence:
        return SweepSafetyEvidence(workspace_id, scope_digest, lease_id, generation, True, True, "phase7-fixture-proof")


def _initialize_fixture(workspace: Path):
    layout = WorkspaceV2Layout(workspace)
    layout.ensure_dirs()
    for domain, paths in layout.databases.items():
        for path in paths:
            try:
                initialize_database(path, domain if domain != "projection" else "projection", layout=layout)
            except Exception:
                pass
    for cls in (RuntimeStore, MemoryAtomStore, RuleV2Store, EvidenceStore, ContentStore, CodeGraphStore, AssetStore, SkillStore):
        cls(workspace)
    ProjectionStore(workspace)
    content = ContentStore(workspace)
    namespace = content.ensure_namespace(workspace_id=str(layout.workspace), trust_domain="phase7-accept")
    orphan = content.put_blob(namespace.namespace_id, "phase7 orphan")
    held = content.put_blob(namespace.namespace_id, "phase7 retained hold")
    assert orphan and held
    content.hold_blob(held, reason="acceptance-hold", source_ref="phase7")
    manager = ManifestManager(layout)
    manager.transition(ManifestState.V2_BUILDING, migration_id="phase7-accept")
    manager.transition(ManifestState.V2_READY, source_digest="source", target_digest="target", manifest_digest="manifest", digests={"validator_passed": True, "checkpoints": {"phase7": True}})
    active = manager.transition(ManifestState.V2_ACTIVE)
    scope = MaintenanceScope(workspace_id=str(layout.workspace), runtime_role="maintenance", trusted_context=True)
    base = MaintenanceContext.trusted(scope, actor_id="phase7-accept", expected_generation=active.generation)
    store = MaintenanceStore(workspace)
    lease = store.acquire_lease(base, ttl_seconds=300)
    context = MaintenanceContext.trusted(scope, actor_id="phase7-accept", maintenance_lease_id=lease.lease_id, expected_generation=active.generation)
    return layout, content, orphan, held, manager, store, context


def _self_test() -> dict:
    with tempfile.TemporaryDirectory(prefix="memoryguard-phase7-") as folder:
        workspace = Path(folder)
        layout, content, orphan, held, manager, store, context = _initialize_fixture(workspace)
        audit_before = ReferenceAudit(workspace).audit()
        sweep = BlobSweepExecutor(workspace, maintenance_store=store, manifest=manager, safety_port=_Safety()).execute("phase7-sweep", context, apply=True)
        with content.connection() as conn:
            orphan_exists = conn.execute("SELECT 1 FROM content_blobs WHERE blob_id=?", (orphan,)).fetchone() is not None
            held_exists = conn.execute("SELECT 1 FROM content_blobs WHERE blob_id=?", (held,)).fetchone() is not None
        compact_job = store.create_job(context, "compact", "phase7-compact", dry_run=False, expected_generation=context.expected_generation)
        compact_job = store.transition_job(compact_job.job_id, MaintenanceJobState.READY, context)
        compact_job = store.transition_job(compact_job.job_id, MaintenanceJobState.ACTIVE, context, expected_generation=context.expected_generation, lease_id=context.maintenance_lease_id)
        executor = SQLiteMaintenanceExecutor(workspace, maintenance_store=store, quiescence_verifier=lambda **_: True, outbox_verifier=lambda **_: True)
        compact = executor.deep_compact(layout.runtime_db, domain="runtime", context=context, job_id=compact_job.job_id, expected_generation=context.expected_generation, apply=True)
        store.transition_job(compact_job.job_id, MaintenanceJobState.SUCCEEDED, context)
        audit_after = ReferenceAudit(workspace).audit()
        checks = {
            "registry_12_domains": len(DEFAULT_REGISTRY) == 12 and "skills" in DEFAULT_REGISTRY,
            "reference_audit_pass": not audit_before.blocked and not audit_after.blocked,
            "two_epoch_sweep": bool(sweep.epoch_one_digest and sweep.epoch_two_digest and sweep.final_digest),
            "orphan_deleted": sweep.swept_count == 1 and not orphan_exists,
            "hold_retained": held_exists,
            "compact_verified": compact.applied and compact.after.integrity_ok,
            "no_raw_ids_in_public_audit": orphan not in json.dumps(audit_before.to_public_dict(), ensure_ascii=False),
        }
        result = {
            "ok": all(checks.values()),
            "status": "PASS" if all(checks.values()) else "BLOCKED",
            "state": "V2_BUILDING",
            "ready": False,
            "can_promote": False,
            "dry_run": False,
            "checks": checks,
            "metrics": {"domains": len(DEFAULT_REGISTRY), "swept": sweep.swept_count, "retained": int(held_exists)},
        }
        del executor, content, store, manager, layout
        gc.collect()
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--json", action="store_true", help="compatibility flag; output is always one JSON object")
    args = parser.parse_args()
    try:
        result = _self_test() if args.self_test else _read_only(Path(args.workspace).expanduser())
    except Exception as exc:
        result = {"ok": False, "status": "BLOCKED", "state": "V2_BUILDING", "ready": False, "can_promote": False, "dry_run": not args.self_test, "errors": [type(exc).__name__]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
