"""Two-epoch, fail-closed Content Blob mark/sweep orchestration.

The executor never trusts caller booleans for writer quiescence or outbox
drain.  A deployment-owned safety port must bind those proofs to the exact
workspace, manifest generation, maintenance scope and lease.  All discovery
is delegated to the read-only 12-domain Reference Audit; the final deletion
transaction rechecks every current Content reference before removing a row.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..cutover_v2.state import CutoverState, RuntimeSnapshot
from ..system.manifest import ManifestManager
from .models import (
    CandidateState,
    MaintenanceConflictError,
    MaintenanceContext,
    MaintenanceJobState,
    MaintenanceOperation,
    stable_digest,
)
from .reference_audit import ReferenceAudit, Result as ReferenceAuditResult
from .registry import DEFAULT_REGISTRY, DomainRegistry
from .store import MaintenanceStore
from .storage_report import (
    StorageReportError,
    _SQLiteIdentityLease,
    _assert_lexical_artifacts,
    _path_identity,
    _sidecar_identities,
)


class BlobSweepError(RuntimeError):
    pass


class BlobSweepBlocked(BlobSweepError):
    pass


class BlobSweepRecoveryRequired(BlobSweepError):
    """A durable deletion intent must be reconciled before another sweep."""

    pass


@dataclass(frozen=True, slots=True)
class SweepSafetyEvidence:
    workspace_id: str
    scope_digest: str
    lease_id: str
    generation: int
    writer_quiesced: bool
    outbox_drained: bool
    proof_digest: str

    def __post_init__(self) -> None:
        for name in ("workspace_id", "scope_digest", "lease_id", "proof_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError(f"{name} is required")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("generation must be a non-negative int")
        if type(self.writer_quiesced) is not bool or type(self.outbox_drained) is not bool:
            raise ValueError("safety evidence flags must be bool")


class SweepSafetyPort(Protocol):
    def verify(
        self,
        *,
        workspace_id: str,
        scope_digest: str,
        lease_id: str,
        generation: int,
    ) -> SweepSafetyEvidence: ...


@dataclass(frozen=True, slots=True)
class BlobSweepResult:
    job_id: str
    status: str
    applied: bool
    epoch_one_digest: str
    epoch_two_digest: str
    final_digest: str
    candidate_count: int
    swept_count: int
    skipped_count: int
    blocker_codes: tuple[str, ...] = ()

    @property
    def dry_run(self) -> bool:
        return not self.applied

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "applied": self.applied,
            "dry_run": self.dry_run,
            "epoch_one_digest": self.epoch_one_digest,
            "epoch_two_digest": self.epoch_two_digest,
            "final_digest": self.final_digest,
            "candidate_count": self.candidate_count,
            "swept_count": self.swept_count,
            "skipped_count": self.skipped_count,
            "blocker_codes": list(self.blocker_codes),
        }


def _audit_digest(result: ReferenceAuditResult) -> str:
    return stable_digest(
        {
            "status": result.status,
            "registry_digest": result.registry_digest,
            "manifest_generation": result.manifest_generation,
            "schema_fingerprints": dict(result.schema_fingerprints),
            "references": [item.to_dict() for item in result.references],
            "candidates": list(result.candidates),
            "blockers": [item.code for item in result.blockers],
        }
    )


def _hold_digest(result: ReferenceAuditResult) -> str:
    return stable_digest(
        sorted(
            (item.source_domain, item.source_table, item.target_id)
            for item in result.references
            if item.kind == "hold" or item.source_table == "content_holds"
        )
    )


class BlobSweepExecutor:
    """Persist two audit epochs and optionally sweep their stable intersection."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        maintenance_store: MaintenanceStore | None = None,
        manifest: ManifestManager | None = None,
        safety_port: SweepSafetyPort | None = None,
        registry: DomainRegistry = DEFAULT_REGISTRY,
        page_size: int = 256,
    ) -> None:
        self.workspace = ReferenceAudit(workspace, registry=registry, page_size=page_size).workspace
        self.registry = registry
        self.page_size = page_size
        self.store = maintenance_store or MaintenanceStore(self.workspace)
        self.manifest = manifest or ManifestManager(self.workspace)
        self.safety_port = safety_port

    def _snapshot(self) -> RuntimeSnapshot:
        try:
            record = self.manifest.current()
            snapshot = RuntimeSnapshot.from_value({"state": record.state.value, "generation": record.generation})
        except Exception as exc:
            raise BlobSweepBlocked("manifest snapshot unavailable") from exc
        if not snapshot.available or not snapshot.trusted or snapshot.state is not CutoverState.V2_ACTIVE:
            raise BlobSweepBlocked("blob sweep requires V2_ACTIVE manifest")
        return snapshot

    def _context(self, context: MaintenanceContext, snapshot: RuntimeSnapshot) -> MaintenanceContext:
        if not isinstance(context, MaintenanceContext) or not context.trusted_context or not context.scope.trusted_context:
            raise BlobSweepBlocked("trusted MaintenanceContext is required")
        if context.workspace_id != str(self.workspace):
            raise BlobSweepBlocked("maintenance workspace mismatch")
        expected = context.expected_generation
        if type(expected) is not int or expected != snapshot.generation:
            raise BlobSweepBlocked("maintenance generation CAS conflict")
        self.store.verify_active_lease(context, expected_generation=expected)
        return context

    def _safety(self, context: MaintenanceContext, generation: int) -> SweepSafetyEvidence:
        if self.safety_port is None:
            raise BlobSweepBlocked("trusted writer/outbox safety verifier is required")
        evidence = self.safety_port.verify(
            workspace_id=str(self.workspace),
            scope_digest=context.scope.digest,
            lease_id=context.maintenance_lease_id,
            generation=generation,
        )
        if not isinstance(evidence, SweepSafetyEvidence):
            raise BlobSweepBlocked("safety verifier returned an invalid receipt")
        if (
            evidence.workspace_id != str(self.workspace)
            or evidence.scope_digest != context.scope.digest
            or evidence.lease_id != context.maintenance_lease_id
            or evidence.generation != generation
            or not evidence.writer_quiesced
            or not evidence.outbox_drained
        ):
            raise BlobSweepBlocked("writer quiescence/outbox evidence does not match the maintenance lease")
        return evidence

    def _audit(self, previous: ReferenceAuditResult | None = None) -> ReferenceAuditResult:
        result = ReferenceAudit(self.workspace, registry=self.registry, page_size=self.page_size).audit(previous=previous)
        if result.blocked:
            raise BlobSweepBlocked("reference audit is blocked: " + ",".join(sorted({item.code for item in result.blockers})))
        if result.manifest_generation is None:
            raise BlobSweepBlocked("reference audit did not bind a manifest generation")
        external = [
            item for item in result.references
            if item.target_domain == "content" and item.target_table == "content_blobs" and item.source_domain != "content"
        ]
        if external:
            raise BlobSweepBlocked("external Blob publishers are not covered by the content transaction")
        return result

    @staticmethod
    def _result_from_report(job_id: str, report: Any) -> BlobSweepResult:
        if report is None or report.status != "PASS":
            raise BlobSweepBlocked("completed sweep has no valid PASS report")
        safety = report.safety
        counts = report.counts
        try:
            applied = safety.get("physical_deletion") is True
            values = {
                "epoch_one_digest": safety.get("epoch_one_digest", ""),
                "epoch_two_digest": safety.get("epoch_two_digest", ""),
                "final_digest": safety.get("final_audit_digest", ""),
            }
            if any(not isinstance(value, str) for value in values.values()):
                raise ValueError("invalid digest")
            candidate_count = counts["stable_candidates"]
            swept_count = counts["swept"]
            if type(candidate_count) is not int or type(swept_count) is not int:
                raise ValueError("invalid count")
            return BlobSweepResult(
                job_id,
                "PASS",
                applied,
                values["epoch_one_digest"],
                values["epoch_two_digest"],
                values["final_digest"],
                candidate_count,
                swept_count,
                candidate_count - swept_count,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BlobSweepBlocked("stored sweep report is malformed") from exc

    def _record_result(
        self,
        job_id: str,
        context: MaintenanceContext,
        *,
        status: str,
        first_digest: str,
        second_digest: str,
        final_digest: str,
        candidate_count: int,
        swept_count: int,
        generation: int,
        applied: bool,
        proof_digest: str = "",
        deletion_digest: str = "",
    ) -> Any:
        return self.store.record_report(
            job_id,
            context,
            status=status,
            counts={
                "stable_candidates": candidate_count,
                "swept": swept_count,
                "retained": candidate_count - swept_count,
            },
            safety={
                "registry_digest": self.registry.digest,
                "manifest_generation": generation,
                "epoch_one_digest": first_digest,
                "epoch_two_digest": second_digest,
                "final_audit_digest": final_digest,
                "proof_digest": proof_digest,
                "deletion_digest": deletion_digest,
                "physical_deletion": applied,
            },
        )

    def _persist_epoch(
        self,
        job_id: str,
        context: MaintenanceContext,
        number: int,
        result: ReferenceAuditResult,
        *,
        confirm: set[str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        digest = _audit_digest(result)
        hold_digest = _hold_digest(result)
        epoch = self.store.begin_epoch(job_id, context, epoch_number=number, reference_digest=digest)
        candidate_ids: dict[str, str] = {}
        for blob_id in result.candidates:
            candidate = self.store.mark_candidate(epoch.epoch_id, blob_id, context, reference_digest=digest, hold_digest=hold_digest)
            candidate_ids[blob_id] = candidate.candidate_id
            if confirm is not None and blob_id in confirm:
                self.store.confirm_candidate(candidate.candidate_id, context, reference_digest=digest, hold_digest=hold_digest)
        self.store.complete_epoch(epoch.epoch_id, context, reference_digest=digest)
        return digest, candidate_ids

    @staticmethod
    def _has_current_reference(conn: Any, blob_id: str) -> bool:
        checks = (
            ("content_occurrences", "blob_id", "active IN (1,'1','true')"),
            ("raw_content", "blob_id", "1=1"),
            ("content_holds", "blob_id", "active IN (1,'1','true')"),
            ("content_tombstones", "blob_id", "active IN (1,'1','true')"),
            ("knowledge_records", "content_blob_id", "1=1"),
            ("migration_map", "target_id", "1=1"),
        )
        names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, column, predicate in checks:
            if table not in names:
                raise BlobSweepBlocked(f"content reference table is missing: {table}")
            if conn.execute(f'SELECT 1 FROM "{table}" WHERE "{column}"=? AND {predicate} LIMIT 1', (blob_id,)).fetchone() is not None:
                return True
        return False

    def _delete(
        self,
        candidates: tuple[str, ...],
        candidate_ids: dict[str, str],
        context: MaintenanceContext,
        generation: int,
        proof: SweepSafetyEvidence,
        *,
        expected_identity: tuple[int, int, int, int] | None = None,
        expected_sidecars: dict[str, tuple[int, int, int, int] | None] | None = None,
    ) -> tuple[int, str]:
        content_path = self.registry.path_for(self.workspace, "content")
        deletion_digest = stable_digest({"candidates": list(candidates), "generation": generation, "proof_digest": proof.proof_digest})
        try:
            expected_identity = expected_identity or _path_identity(content_path)
            expected_sidecars = expected_sidecars or _sidecar_identities(content_path)
            try:
                lease = _SQLiteIdentityLease.open(
                    content_path,
                    readonly=False,
                    expected_identity=expected_identity,
                    expected_sidecars=expected_sidecars,
                )
            except StorageReportError:
                # A legitimate writer may rotate/remove WAL sidecars while
                # retaining the authoritative DB inode (e.g. a hold inserted
                # between final audit and delete).  Preserve the main-file
                # compare-and-swap and lexical checks, then retry against the
                # current sidecar set.  If the main file did not change at
                # all, a sidecar swap remains a hard identity failure.
                current_identity = _path_identity(content_path)
                if (
                    expected_identity is None
                    or current_identity[:2] != expected_identity[:2]
                    or current_identity == expected_identity
                ):
                    raise
                _assert_lexical_artifacts(content_path)
                _sidecar_identities(content_path)
                lease = _SQLiteIdentityLease.open(
                    content_path,
                    readonly=False,
                    expected_identity=expected_identity,
                )
        except (OSError, ValueError, StorageReportError) as exc:
            raise BlobSweepBlocked("content database identity is unavailable") from exc
        intents: list[str] = []
        with lease as conn:
            try:
                lease.assert_current()
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                # SQLite may create/rotate/delete WAL, SHM, or journal
                # sidecars while enabling WAL.  This is a trusted local
                # operation, so record the resulting sidecar set before the
                # next lease assertion; unannounced changes remain fail
                # closed.
                lease.refresh_sidecars()
                lease.identity = _path_identity(content_path)
                for blob_id in candidates:
                    candidate_id = candidate_ids.get(blob_id)
                    if not candidate_id:
                        raise BlobSweepBlocked("candidate disappeared before deletion intent")
                    self.store.begin_candidate_sweep(
                        candidate_id,
                        context,
                        expected_generation=generation,
                        deletion_digest=deletion_digest,
                    )
                    intents.append(candidate_id)
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN IMMEDIATE")
                for blob_id in candidates:
                    if self._has_current_reference(conn, blob_id):
                        raise BlobSweepBlocked("candidate gained a reference during the final sweep CAS")
                    changed = conn.execute("DELETE FROM content_blobs WHERE blob_id=?", (blob_id,))
                    if changed.rowcount != 1:
                        raise BlobSweepBlocked("candidate disappeared before the final sweep CAS")
                fk = tuple(conn.execute("PRAGMA foreign_key_check").fetchall())
                integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
                if fk or integrity != "ok":
                    raise BlobSweepBlocked("post-sweep content integrity check failed")
                conn.commit()
                # COMMIT can finish a WAL transaction and rotate/remove
                # sidecars.  Refresh only after the transaction and its
                # integrity checks succeeded, then assert the refreshed
                # identity lease before leaving the SQLite window.
                lease.refresh_sidecars()
                lease.assert_current()
            except Exception as exc:
                try:
                    conn.rollback()
                finally:
                    rollback_digest = stable_digest({"deletion_digest": deletion_digest, "reason": type(exc).__name__})
                    failed = 0
                    for candidate_id in reversed(intents):
                        try:
                            self.store.restore_candidate_confirmed(
                                candidate_id,
                                context,
                                expected_generation=generation,
                                rollback_digest=rollback_digest,
                            )
                        except Exception:
                            failed += 1
                    if failed:
                        raise BlobSweepRecoveryRequired("content rollback succeeded but deletion intents require reconciliation") from exc
                raise

        try:
            for candidate_id in intents:
                self.store.mark_candidate_swept(
                    candidate_id,
                    context,
                    expected_generation=generation,
                    deletion_digest=deletion_digest,
                )
        except Exception as exc:
            # Content is already durably committed.  Leave the ACTIVE job and
            # DELETING intents intact so an identical replay can reconcile it.
            raise BlobSweepRecoveryRequired("content deletion committed; maintenance receipt requires reconciliation") from exc
        return len(intents), deletion_digest

    def _recover_active_job(
        self,
        job: Any,
        context: MaintenanceContext,
        snapshot: RuntimeSnapshot,
    ) -> BlobSweepResult:
        ready = self.store.get_latest_job_report(job.job_id, context, status="READY")
        if ready is None:
            raise BlobSweepRecoveryRequired("ACTIVE sweep has no durable pre-delete report")
        candidates = self.store.list_job_candidates(job.job_id, context, epoch_number=2)
        if any(item.state not in {CandidateState.CONFIRMED, CandidateState.DELETING, CandidateState.SWEPT} for item in candidates):
            raise BlobSweepRecoveryRequired("ACTIVE sweep candidate state is not recoverable")
        self._safety(context, snapshot.generation)
        content_path = self.registry.path_for(self.workspace, "content")
        try:
            expected_identity = _path_identity(content_path)
            expected_sidecars = _sidecar_identities(content_path)
        except (OSError, ValueError, StorageReportError) as exc:
            raise BlobSweepRecoveryRequired("content database identity is unavailable") from exc
        self._audit()
        if _path_identity(content_path)[:2] != expected_identity[:2]:
            raise BlobSweepRecoveryRequired("content database identity changed during recovery audit")
        try:
            lease = _SQLiteIdentityLease.open(content_path, readonly=True, expected_identity=expected_identity, expected_sidecars=expected_sidecars)
        except (OSError, ValueError, StorageReportError) as exc:
            raise BlobSweepRecoveryRequired("content database identity is unavailable") from exc
        with lease as conn:
            try:
                lease.assert_current()
                present = {
                    item.candidate_id: conn.execute(
                        "SELECT 1 FROM content_blobs WHERE blob_id=? LIMIT 1",
                        (item.blob_id,),
                    ).fetchone() is not None
                    for item in candidates
                }
                lease.assert_current()
            except Exception as exc:
                raise BlobSweepRecoveryRequired("content database identity changed during recovery") from exc
        present_count = sum(present.values())
        if present_count not in {0, len(candidates)}:
            raise BlobSweepRecoveryRequired("mixed content/maintenance sweep state requires operator recovery")
        safety = ready.safety
        deletion_digest = str(safety.get("deletion_digest", ""))
        if not deletion_digest:
            raise BlobSweepRecoveryRequired("ACTIVE sweep is missing deletion digest")
        if present_count:
            rollback_digest = stable_digest({"deletion_digest": deletion_digest, "reason": "replay_before_content_commit"})
            for item in candidates:
                if item.state is CandidateState.SWEPT:
                    raise BlobSweepRecoveryRequired("swept receipt exists while content Blob is still present")
                if item.state is CandidateState.DELETING:
                    self.store.restore_candidate_confirmed(item.candidate_id, context, expected_generation=snapshot.generation, rollback_digest=rollback_digest)
            self.store.transition_job(job.job_id, MaintenanceJobState.FAILED, context)
            self.store.record_report(job.job_id, context, status="BLOCKED", counts={"swept": 0}, safety={"error_code": "recovered_before_content_commit", "physical_deletion": False})
            raise BlobSweepBlocked("prior sweep stopped before the content transaction committed")
        for item in candidates:
            if item.state is CandidateState.CONFIRMED:
                raise BlobSweepRecoveryRequired("content Blob is missing without a deletion intent")
            if item.state is CandidateState.DELETING:
                self.store.mark_candidate_swept(item.candidate_id, context, expected_generation=snapshot.generation, deletion_digest=deletion_digest)
        report = self._record_result(
            job.job_id,
            context,
            status="PASS",
            first_digest=str(safety.get("epoch_one_digest", "")),
            second_digest=str(safety.get("epoch_two_digest", "")),
            final_digest=str(safety.get("final_audit_digest", "")),
            candidate_count=len(candidates),
            swept_count=len(candidates),
            generation=snapshot.generation,
            applied=True,
            proof_digest=str(safety.get("proof_digest", "")),
            deletion_digest=deletion_digest,
        )
        self.store.transition_job(job.job_id, MaintenanceJobState.SUCCEEDED, context)
        return self._result_from_report(job.job_id, report)

    def execute(self, request_key: str, context: MaintenanceContext, *, apply: bool = False) -> BlobSweepResult:
        if type(apply) is not bool:
            raise ValueError("apply must be bool")
        snapshot = self._snapshot()
        ctx = self._context(context, snapshot)
        job = self.store.create_job(ctx, MaintenanceOperation.SWEEP, request_key=request_key, dry_run=not apply, expected_generation=snapshot.generation)
        if job.state is MaintenanceJobState.SUCCEEDED:
            return self._result_from_report(job.job_id, self.store.get_latest_job_report(job.job_id, ctx, status="PASS"))
        if job.state is MaintenanceJobState.ACTIVE:
            return self._recover_active_job(job, ctx, snapshot)
        if job.state is not MaintenanceJobState.PLANNED:
            raise MaintenanceConflictError("sweep request replay is not resumable")
        self.store.transition_job(job.job_id, MaintenanceJobState.AUDITING, ctx)
        first_digest = second_digest = final_digest = ""
        try:
            first = self._audit()
            if first.manifest_generation != snapshot.generation:
                raise BlobSweepBlocked("manifest generation changed before epoch one")
            first_digest, _ = self._persist_epoch(job.job_id, ctx, 1, first)
            second = self._audit(previous=first)
            if second.manifest_generation != snapshot.generation:
                raise BlobSweepBlocked("manifest generation changed before epoch two")
            stable = set(first.candidates) & set(second.candidates)
            second_digest, candidate_ids = self._persist_epoch(job.job_id, ctx, 2, second, confirm=stable)
            self.store.transition_job(job.job_id, MaintenanceJobState.READY, ctx)
            if not apply:
                report = self._record_result(job.job_id, ctx, status="PASS", first_digest=first_digest, second_digest=second_digest, final_digest="", candidate_count=len(stable), swept_count=0, generation=snapshot.generation, applied=False)
                self.store.transition_job(job.job_id, MaintenanceJobState.SUCCEEDED, ctx)
                return self._result_from_report(job.job_id, report)
            self.store.transition_job(job.job_id, MaintenanceJobState.ACTIVE, ctx, expected_generation=snapshot.generation, lease_id=ctx.maintenance_lease_id)
            proof = self._safety(ctx, snapshot.generation)
            content_path = self.registry.path_for(self.workspace, "content")
            try:
                content_identity = _path_identity(content_path)
                content_sidecars = _sidecar_identities(content_path)
            except (OSError, ValueError, StorageReportError) as exc:
                raise BlobSweepBlocked("content database identity is unavailable") from exc
            final = self._audit(previous=second)
            final_digest = _audit_digest(final)
            if _path_identity(content_path)[:2] != content_identity[:2]:
                raise BlobSweepBlocked("content database identity changed during final audit")
            if final.manifest_generation != snapshot.generation or set(final.candidates) & stable != stable:
                raise BlobSweepBlocked("candidate/reference state changed after epoch two")
            # Recheck the trusted safety port after the final audit.  The port
            # owns the writer barrier; a changed receipt invalidates the run.
            proof_after = self._safety(ctx, snapshot.generation)
            if proof_after.proof_digest != proof.proof_digest:
                raise BlobSweepBlocked("writer/outbox proof changed before deletion")
            ordered = tuple(sorted(stable))
            deletion_digest = stable_digest({"candidates": list(ordered), "generation": snapshot.generation, "proof_digest": proof.proof_digest})
            self._record_result(job.job_id, ctx, status="READY", first_digest=first_digest, second_digest=second_digest, final_digest=final_digest, candidate_count=len(stable), swept_count=0, generation=snapshot.generation, applied=False, proof_digest=proof.proof_digest, deletion_digest=deletion_digest)
            swept, deletion_digest = self._delete(
                ordered,
                candidate_ids,
                ctx,
                snapshot.generation,
                proof,
                expected_identity=content_identity,
                expected_sidecars=content_sidecars,
            )
            report = self._record_result(job.job_id, ctx, status="PASS", first_digest=first_digest, second_digest=second_digest, final_digest=final_digest, candidate_count=len(stable), swept_count=swept, generation=snapshot.generation, applied=True, proof_digest=proof.proof_digest, deletion_digest=deletion_digest)
            self.store.transition_job(job.job_id, MaintenanceJobState.SUCCEEDED, ctx)
            return self._result_from_report(job.job_id, report)
        except Exception as exc:
            current = self.store.get_job(job.job_id)
            recovery_required = isinstance(exc, BlobSweepRecoveryRequired)
            if not recovery_required and current is not None and current.state in {MaintenanceJobState.AUDITING, MaintenanceJobState.READY, MaintenanceJobState.ACTIVE}:
                try:
                    self.store.transition_job(job.job_id, MaintenanceJobState.FAILED, ctx)
                except Exception:
                    pass
            try:
                self.store.record_report(job.job_id, ctx, status="RECOVERY_REQUIRED" if recovery_required else "BLOCKED", counts={"swept": 0}, safety={"error_code": type(exc).__name__, "physical_deletion": False})
            except Exception:
                pass
            if isinstance(exc, BlobSweepError):
                raise
            raise BlobSweepError("blob sweep failed") from exc

    run = execute
    sweep = execute


__all__ = [
    "BlobSweepError", "BlobSweepBlocked", "BlobSweepRecoveryRequired", "SweepSafetyEvidence", "SweepSafetyPort",
    "BlobSweepResult", "BlobSweepExecutor",
]
