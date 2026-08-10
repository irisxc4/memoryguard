"""Native V2 dispatch port for the Phase 7 maintenance CLI surface."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .api import MaintenanceV2Api
from .models import MaintenanceContext, MaintenanceJobState, MaintenanceOperation, MaintenanceScope, stable_digest
from .store import MaintenanceStore


_TRANSPORT_CAPABILITY = object()


def bind_maintenance_transport_context(values: Mapping[str, Any]) -> dict[str, Any]:
    """Attach a process-local capability at the trusted CLI transport edge."""

    result = dict(values)
    result["__maintenance_transport_capability"] = _TRANSPORT_CAPABILITY
    return result


class MaintenanceRuntimePort:
    supports_rule_mutation_context = True

    def __init__(
        self,
        workspace: str | Path,
        *,
        sweep_safety_port: Any = None,
        quiescence_verifier: Any = None,
        outbox_verifier: Any = None,
    ) -> None:
        self.workspace = str(Path(workspace).expanduser().resolve())
        self.store: MaintenanceStore | None = None
        self.api = MaintenanceV2Api(
            self.workspace,
            sweep_safety_port=sweep_safety_port,
            quiescence_verifier=quiescence_verifier,
            outbox_verifier=outbox_verifier,
        )

    @staticmethod
    def _payload(args: Any) -> dict[str, Any]:
        if args is None:
            return {}
        if isinstance(args, Mapping):
            return {str(key): value for key, value in args.items() if str(key) != "func"}
        try:
            return {str(key): value for key, value in vars(args).items() if str(key) != "func"}
        except TypeError as exc:
            raise ValueError("CLI arguments must be a mapping or Namespace") from exc

    @staticmethod
    def _error(code: str) -> dict[str, Any]:
        return {"ok": False, "status": "error", "code": code, "error": code}

    def _public_action(self, result: Any, domain: str) -> dict[str, Any]:
        payload = result.to_dict()
        label = (Path(self.api.registry[domain].relative_path) / self.api.registry[domain].db_name).as_posix()
        payload["path"] = label
        payload["temp_path"] = ""
        for key in ("before", "after"):
            if isinstance(payload.get(key), dict):
                payload[key]["path"] = label
        return payload

    @staticmethod
    def _compact_receipt(job_id: str, report: Any) -> dict[str, Any]:
        if report is None or report.status != "PASS":
            raise ValueError("compact PASS report is missing")
        return {
            "job_id": job_id,
            "operation": "compact",
            "status": "PASS",
            "applied": True,
            "domain": str(report.safety.get("domain", "")),
            "before_digest": str(report.safety.get("before_digest", "")),
            "after_digest": str(report.safety.get("after_digest", "")),
        }

    def _context(self, raw: Any, payload: Mapping[str, Any], generation: int, *, lease_required: bool) -> MaintenanceContext:
        if not isinstance(raw, Mapping) or raw.get("__maintenance_transport_capability") is not _TRANSPORT_CAPABILITY:
            raise ValueError("trusted maintenance transport context is required")
        actor = raw.get("trusted_agent_id") or raw.get("session_id")
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("trusted maintenance actor is required")
        lease_id = payload.get("lease_id", "")
        if not isinstance(lease_id, str) or (lease_required and not lease_id.strip()):
            raise ValueError("maintenance lease_id is required")
        scope = MaintenanceScope(
            workspace_id=self.workspace,
            agent_instance_id=str(raw.get("trusted_agent_id") or ""),
            runtime_role="maintenance",
            trusted_context=True,
        )
        return MaintenanceContext.trusted(
            scope,
            actor_id=actor.strip(),
            maintenance_lease_id=lease_id.strip(),
            expected_generation=generation,
        )

    def _store(self) -> MaintenanceStore:
        if self.store is None:
            self.store = MaintenanceStore(self.workspace)
            self.api.store = self.store
        return self.store

    def dispatch(self, surface: str, name: str, args: Any, *, context: Any = None, generation: int, mutation: bool = False) -> dict[str, Any]:
        if surface != "cli" or name != "storage":
            return self._error("v2_operation_not_implemented")
        if type(generation) is not int or generation < 0:
            return self._error("invalid_manifest_generation")
        try:
            payload = self._payload(args)
            action = str(payload.get("action") or "").casefold()
            if action == "audit":
                result = self.api.audit_references()
                return {"ok": not result["blocked"], "status": result["status"].casefold(), "data": result}
            if action == "report":
                return {"ok": True, "status": "ok", "data": self.api.storage_report(str(payload.get("domain") or ""))}
            if action == "lease-acquire":
                ctx = self._context(context, payload, generation, lease_required=False)
                lease = self._store().acquire_lease(ctx, ttl_seconds=int(payload.get("ttl_seconds") or 300))
                return {"ok": True, "status": "ok", "data": {"lease_id": lease.lease_id, "expires_at": lease.expires_at}}
            if action == "lease-release":
                ctx = self._context(context, payload, generation, lease_required=True)
                lease = self._store().release_lease(ctx)
                return {"ok": True, "status": "ok", "data": {"released": not lease.active}}
            if action == "sweep":
                if payload.get("apply") is True and self.api.sweep_safety_port is None:
                    return self._error("sweep_safety_verifier_unavailable")
                ctx = self._context(context, payload, generation, lease_required=True)
                result = self.api.sweep(str(payload.get("request_key") or ""), ctx, apply=payload.get("apply") is True)
                return {"ok": result.status == "PASS", "status": result.status.casefold(), "data": result.to_dict()}
            if action == "compact":
                apply = payload.get("apply") is True
                if not apply:
                    domain = str(payload.get("domain") or "")
                    target = self.api.registry.path_for(self.api.workspace, domain)
                    from .sqlite_maintenance import SQLiteMaintenanceExecutor

                    result = SQLiteMaintenanceExecutor(self.api.workspace).deep_compact(target, domain=domain)
                    return {"ok": True, "status": "ok", "data": self._public_action(result, domain)}
                if self.api.quiescence_verifier is None or self.api.outbox_verifier is None:
                    return self._error("compact_safety_verifier_unavailable")
                ctx = self._context(context, payload, generation, lease_required=True)
                request_key = str(payload.get("request_key") or "")
                store = self._store()
                job = store.create_job(ctx, MaintenanceOperation.COMPACT, request_key=request_key, dry_run=False, expected_generation=generation)
                if job.state is MaintenanceJobState.SUCCEEDED:
                    report = store.get_latest_job_report(job.job_id, ctx, status="PASS")
                    return {"ok": True, "status": "ok", "data": self._compact_receipt(job.job_id, report)}
                if job.state is MaintenanceJobState.PLANNED:
                    job = store.transition_job(job.job_id, MaintenanceJobState.READY, ctx)
                if job.state is MaintenanceJobState.READY:
                    job = store.transition_job(job.job_id, MaintenanceJobState.ACTIVE, ctx, expected_generation=generation, lease_id=ctx.maintenance_lease_id)
                if job.state is not MaintenanceJobState.ACTIVE:
                    return self._error("compact_job_not_active")
                prior = store.get_latest_job_report(job.job_id, ctx, status="PASS")
                if prior is not None:
                    try:
                        store.transition_job(job.job_id, MaintenanceJobState.SUCCEEDED, ctx)
                    except Exception:
                        return self._error("compact_receipt_recovery_required")
                    return {"ok": True, "status": "ok", "data": self._compact_receipt(job.job_id, prior)}
                try:
                    result = self.api.compact(str(payload.get("domain") or ""), ctx, job_id=job.job_id, apply=True)
                except Exception:
                    current = store.get_job(job.job_id)
                    if current is not None and current.state is MaintenanceJobState.ACTIVE:
                        store.transition_job(job.job_id, MaintenanceJobState.FAILED, ctx)
                    raise
                domain = str(payload.get("domain") or "")
                before_payload = result.before.to_dict(); before_payload.pop("path", None)
                after_payload = result.after.to_dict(); after_payload.pop("path", None)
                try:
                    report = store.record_report(
                        job.job_id,
                        ctx,
                        status="PASS",
                        counts={"allocated_before": result.before.allocated_bytes, "allocated_after": result.after.allocated_bytes},
                        safety={
                            "domain": domain,
                            "before_digest": stable_digest(before_payload),
                            "after_digest": stable_digest(after_payload),
                            "schema_fingerprint": result.after.schema_fingerprint,
                            "physical_compaction": True,
                        },
                    )
                    store.transition_job(job.job_id, MaintenanceJobState.SUCCEEDED, ctx)
                except Exception:
                    # The SQLite rewrite is already durable.  Keep the job
                    # ACTIVE so an identical replay can finish the receipt.
                    return self._error("compact_receipt_recovery_required")
                return {"ok": True, "status": "ok", "data": self._compact_receipt(job.job_id, report)}
            return self._error("unknown_storage_action")
        except Exception as exc:
            return self._error(type(exc).__name__)


NativeV2RuntimePort = MaintenanceRuntimePort

__all__ = ["MaintenanceRuntimePort", "NativeV2RuntimePort", "bind_maintenance_transport_context"]
