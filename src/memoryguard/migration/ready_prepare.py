"""Production V2 shadow -> readiness -> V2_READY orchestration.

This module deliberately stops at ``V2_READY``.  It never calls an activation
API.  Production activation remains a separate, explicit user-approved step.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..cutover_v2.evidence_assembler import ReadinessEvidenceAssembler
from ..cutover_v2.facade import get_v2_runtime_facade
from ..data_home import resolve_data_home
from ..runtime_v2.phase4_acceptance import phase4_acceptance_evidence
from ..system.manifest import ManifestManager, ManifestState
from .workspace_prepare import WorkspacePrepareError, prepare_v2_workspace


READY_SCHEMA = "memoryguard-v2-ready-prepare-1"


def _blocked(stage: str, *, build: Any = None, readiness: Any = None, error: str = "") -> dict[str, Any]:
    return {
        "schema": READY_SCHEMA,
        "status": "BLOCKED",
        "ok": False,
        "ready": False,
        "manifest_state": "",
        "v2_active": False,
        "activation_required": False,
        "stage": stage,
        "error": str(error or ""),
        "build": build if isinstance(build, dict) else {},
        "readiness": readiness if isinstance(readiness, dict) else {},
    }


def prepare_v2_ready(
    workspace: str | Path,
    *,
    apply: bool = False,
    data_home: str | Path | None = None,
    migration_id: str | None = None,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    """Safely build and, when fully proven, stop at ``V2_READY``.

    ``apply=False`` delegates to the zero-write workspace preparation plan and
    cannot transition the manifest.  ``apply=True`` performs the frozen source
    build, re-runs production readiness (including a second live-source drift
    check), and uses :meth:`ManifestManager.mark_v2_ready` for a third drift
    check immediately before write-freeze.  It never activates V2.
    """

    root = Path(workspace).expanduser().resolve()
    resolved_data_home = Path(data_home).expanduser().resolve() if data_home is not None else resolve_data_home()
    if not apply:
        try:
            build = prepare_v2_workspace(
                root,
                apply=False,
                data_home=resolved_data_home,
                migration_id=migration_id,
                expected_generation=expected_generation,
            )
        except WorkspacePrepareError as exc:
            # A live V1 WAL cannot be read as an immutable zero-write snapshot.
            # This is not an unsafe workspace and does not mean --apply will
            # fail: apply uses SQLite online backup specifically to capture it.
            if "non-empty WAL" not in str(exc):
                raise
            current = ManifestManager(root).current(immutable=True)
            return {
                "schema": READY_SCHEMA,
                "status": "DRY_RUN_REQUIRES_SNAPSHOT",
                "ok": True,
                "ready": False,
                "manifest_state": current.state.value,
                "v2_active": False,
                "activation_required": False,
                "stage": "plan",
                "reason": "live_wal_requires_online_snapshot",
                "build": {"status": "NOT_EVALUATED", "writes_performed": False},
                "readiness": {},
            }
        return {
            "schema": READY_SCHEMA,
            "status": "DRY_RUN",
            "ok": bool(build.get("ok")),
            "ready": False,
            "manifest_state": str(build.get("plan", {}).get("manifest_state") or "V1_ACTIVE"),
            "v2_active": False,
            "activation_required": False,
            "stage": "plan",
            "build": build,
            "readiness": {},
        }

    build = prepare_v2_workspace(
        root,
        apply=True,
        data_home=resolved_data_home,
        migration_id=migration_id,
        expected_generation=expected_generation,
    )
    if build.get("ok") is not True:
        return _blocked("shadow_build", build=build)

    manager = ManifestManager(root)
    building = manager.current()
    if building.state is not ManifestState.V2_BUILDING:
        return _blocked("manifest_building", build=build, error="manifest_not_v2_building")

    phase4 = phase4_acceptance_evidence()
    if phase4.get("ok") is not True:
        return _blocked("phase4", build=build, error="phase4_acceptance_failed")

    native = get_v2_runtime_facade(str(root)).ports.v2
    assembly = ReadinessEvidenceAssembler(
        root,
        data_home=resolved_data_home,
        phase4_evidence=phase4,
        native_coverage=native,
        manifest_manager=manager,
        require_frozen_sources=True,
    ).assemble()
    public_readiness = assembly.to_public_dict()
    if not assembly.ready:
        return _blocked("readiness", build=build, readiness=public_readiness)

    payload = dict(assembly.transition_payload)
    if not payload:
        return _blocked("readiness_transition", build=build, readiness=public_readiness, error="missing_ready_transition_payload")
    ready = manager.mark_v2_ready(**payload)
    if ready.state is not ManifestState.V2_READY:
        return _blocked("readiness_transition", build=build, readiness=public_readiness, error="manifest_not_v2_ready")

    return {
        "schema": READY_SCHEMA,
        "status": "V2_READY",
        "ok": True,
        "ready": True,
        "manifest_state": ready.state.value,
        "generation": ready.generation,
        "migration_id": ready.migration_id,
        "v2_active": False,
        "activation_required": True,
        "stage": "ready",
        "build": {
            "status": build.get("status"),
            "live_source_verification": build.get("live_source_verification", {}),
            "archived_shadow_count": len(build.get("archived_shadow", []) or []),
        },
        "readiness": public_readiness,
    }


__all__ = ["READY_SCHEMA", "prepare_v2_ready"]
