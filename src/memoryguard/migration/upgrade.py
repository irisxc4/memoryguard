"""Public, fail-closed V1 -> V2 upgrade orchestration.

The operator-facing ``memoryguard-v2`` command remains available for the
individual lifecycle primitives.  This module is the public product path for
the common upgrade: preview, prepare the V2 shadow, migrate GUI control
metadata, verify the complete ready evidence, and only then optionally
activate after an exact ``V2_ACTIVE`` confirmation.

No legacy runtime adapter is used here.  A missing manifest is the normal
``V1_ACTIVE`` starting point and is handled without creating anything during a
preview.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from ..cutover_v2.evidence_assembler import ReadinessEvidenceAssembler
from ..cutover_v2.facade import get_v2_runtime_facade
from ..data_home import resolve_data_home
from ..migration.v2_validator import V2MigrationValidator
from ..runtime_v2.group_native import GroupControlService, SystemControlStore
from ..runtime_v2.phase4_acceptance import phase4_acceptance_evidence
from ..storage.transaction import transaction
from ..system.manifest import ManifestManager, ManifestState
from .gui_control import (
    GuiControlMigrationError,
    inspect_legacy_gui_control,
    migrate_legacy_gui_control,
)
from .ready_prepare import prepare_v2_ready
from .workspace_prepare import prepare_v2_workspace, verify_v2_source_snapshot


SCHEMA = "memoryguard-public-upgrade-1"
CONFIRM_ACTIVE = "V2_ACTIVE"
_STAGE_NAMES = ("preflight", "prepare", "gui_control", "verify", "activate")


def _stage(
    status: str = "NOT_RUN",
    *,
    ok: bool = False,
    writes_performed: bool = False,
    code: str = "",
    detail: Any = None,
) -> dict[str, Any]:
    return {
        "status": str(status),
        "ok": bool(ok),
        "writes_performed": bool(writes_performed),
        "code": str(code or ""),
        "detail": detail if detail is not None else {},
    }


def _stages() -> dict[str, dict[str, Any]]:
    return {name: _stage() for name in _STAGE_NAMES}


def _manifest_summary(manager: ManifestManager, current: Any) -> dict[str, Any]:
    return {
        "exists": bool(manager.exists()),
        "state": str(getattr(getattr(current, "state", None), "value", "UNKNOWN")),
        "generation": getattr(current, "generation", None),
        "migration_id": str(getattr(current, "migration_id", "") or ""),
    }


def _state_value(current: Any) -> str:
    return str(getattr(getattr(current, "state", None), "value", "UNKNOWN"))


def _error_code(exc: BaseException, default: str = "upgrade_failed") -> str:
    explicit = str(getattr(exc, "code", "") or "").strip()
    if explicit:
        return explicit
    text = str(exc).casefold()
    if "generation conflict" in text:
        return "manifest_generation_conflict"
    if "snapshot" in text and "missing" in text:
        return "v2_snapshot_missing"
    if "source drift" in text or "drift" in text:
        return "v2_source_drift"
    if "symlink" in text or "reparse" in text:
        return "unsafe_path"
    if "manifest" in text and ("missing" in text or "unread" in text or "invalid" in text):
        return "v2_manifest_unavailable"
    return default


def _next_step(kind: str) -> str:
    if kind == "preview":
        return "review the zero-write plan, then rerun with --apply"
    if kind == "ready":
        return "rerun with --apply --confirm V2_ACTIVE to explicitly activate"
    if kind == "resume":
        return "fix the reported issue, then rerun with --apply; the V2_READY batch is resumable"
    if kind == "active":
        return "none; V2 is already active"
    if kind == "confirmation":
        return "rerun with the exact confirmation word V2_ACTIVE"
    return "inspect the stage detail, fix the issue, and rerun the upgrade"


def _envelope(
    *,
    workspace: Path,
    data_home: Path,
    apply: bool,
    current: Any,
    stages: Mapping[str, Any],
    status: str,
    ok: bool,
    stage: str,
    code: str = "",
    next_step: str = "",
    writes_performed: bool = False,
    activation_required: bool = False,
    detail: Any = None,
) -> dict[str, Any]:
    state = _state_value(current)
    return {
        "schema": SCHEMA,
        "command": "upgrade",
        "from_version": "0.6.2",
        "to_runtime": "v2",
        "status": str(status),
        "ok": bool(ok),
        "stage": str(stage),
        "phase": str(stage),
        "code": str(code or ""),
        "next_step": str(next_step or ""),
        "workspace": str(workspace),
        "data_home": str(data_home),
        "workspace_equals_data_home": workspace == data_home,
        "state": state,
        "generation": getattr(current, "generation", None),
        "migration_id": str(getattr(current, "migration_id", "") or ""),
        "apply": bool(apply),
        "writes_performed": bool(writes_performed),
        "activation_required": bool(activation_required),
        "v2_active": state == ManifestState.V2_ACTIVE.value,
        "stages": {name: dict(stages.get(name, _stage())) for name in _STAGE_NAMES},
        "detail": detail if detail is not None else {},
    }


def _legacy_binding_ids(workspace: Path) -> set[str]:
    root = workspace / ".memoryguard" / "agent-bindings"
    result: set[str] = set()
    if not root.is_dir():
        return result
    for path in sorted(root.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, Mapping):
            binding_id = str(raw.get("binding_id") or "").strip()
            if binding_id:
                result.add(binding_id)
    return result


def _verify_ready(
    workspace: Path,
    data_home: Path,
    manager: ManifestManager,
    control_preview: Mapping[str, Any],
) -> dict[str, Any]:
    """Re-run the production readiness evidence after GUI control migration."""

    current = manager.current()
    if current.state not in {ManifestState.V2_BUILDING, ManifestState.V2_READY}:
        return {
            "ok": False,
            "status": "BLOCKED",
            "code": "verification_requires_v2_ready",
            "state": _state_value(current),
        }

    legacy_ids = _legacy_binding_ids(workspace)
    bindings = GroupControlService(workspace, write=False).list_bindings(include_inactive=True)
    v2_ids = {
        str(item.get("binding_id") or "")
        for item in bindings.get("bindings", [])
        if isinstance(item, Mapping)
    }
    missing_bindings = sorted(legacy_ids - v2_ids)
    control = {
        "legacy_record_count": int(control_preview.get("record_count") or 0),
        "v2_binding_count": len(v2_ids),
        "missing_binding_ids": missing_bindings,
        "status": "PASS" if not missing_bindings else "BLOCKED",
    }
    if missing_bindings:
        return {
            "ok": False,
            "status": "BLOCKED",
            "code": "gui_control_verification_failed",
            "control": control,
        }

    # A V2_READY batch may have been produced by the hidden operator command
    # before this public flow was introduced.  Its readiness digest is already
    # immutable, so re-running the full assembler after adding GUI control
    # metadata would report a legitimate digest change rather than a resumable
    # migration.  Re-verify the frozen live source, V2 target validator and
    # migrated control records without attempting to rewrite READY evidence.
    if current.state is ManifestState.V2_READY:
        phase2 = current.checkpoints.get("phase2_sources", {}) if isinstance(current.checkpoints, Mapping) else {}
        snapshot = phase2.get("snapshot", {}) if isinstance(phase2, Mapping) else {}
        if not isinstance(snapshot, Mapping) or str(snapshot.get("mode") or "") != "frozen":
            return {
                "ok": False,
                "status": "BLOCKED",
                "code": "v2_snapshot_missing",
                "control": control,
            }
        try:
            source_verification = verify_v2_source_snapshot(
                workspace,
                data_home=data_home,
                migration_id=current.migration_id,
            )
            source_workspace = str(snapshot.get("workspace") or "")
            raw_source_data_home = str(snapshot.get("data_home") or "")
            validator = V2MigrationValidator(
                workspace,
                data_home=data_home,
                migration_id=current.migration_id,
                expected_source_hashes=(phase2.get("hashes", {}) if isinstance(phase2, Mapping) else {}),
                source_workspace=source_workspace,
                source_data_home=(None if raw_source_data_home in {"", "NOT_CONFIGURED"} else raw_source_data_home),
            )
            validation = validator.validate(migration_id=current.migration_id).to_dict()
        except Exception as exc:
            return {
                "ok": False,
                "status": "BLOCKED",
                "code": _error_code(exc, "v2_ready_verification_failed"),
                "control": control,
                "error": str(exc),
            }
        if source_verification.get("activation_safe") is not True or validation.get("ok") is not True:
            return {
                "ok": False,
                "status": "BLOCKED",
                "code": "v2_ready_verification_failed",
                "control": control,
                "source_verification": source_verification,
                "validation": validation,
            }
        return {
            "ok": True,
            "status": "PASS",
            "code": "",
            "verification_mode": "resumed_v2_ready",
            "control": control,
            "source_verification": source_verification,
            "validation": validation,
        }

    phase4 = phase4_acceptance_evidence()
    if phase4.get("ok") is not True:
        return {
            "ok": False,
            "status": "BLOCKED",
            "code": "phase4_acceptance_failed",
            "control": control,
            "phase4": phase4,
        }

    try:
        native = get_v2_runtime_facade(str(workspace)).ports.v2
        assembly = ReadinessEvidenceAssembler(
            workspace,
            data_home=data_home,
            phase4_evidence=phase4,
            native_coverage=native,
            manifest_manager=manager,
            require_frozen_sources=True,
        ).assemble()
        readiness = assembly.to_public_dict()
    except Exception as exc:  # stable public error; no traceback in CLI output
        return {
            "ok": False,
            "status": "BLOCKED",
            "code": _error_code(exc, "v2_readiness_verification_failed"),
            "control": control,
            "error": str(exc),
        }

    ready = bool(getattr(assembly, "ready", False))
    blockers = readiness.get("blockers", []) if isinstance(readiness, Mapping) else []
    if ready and current.state is ManifestState.V2_BUILDING:
        payload = dict(getattr(assembly, "transition_payload", {}) or {})
        if not payload:
            return {
                "ok": False,
                "status": "BLOCKED",
                "code": "missing_ready_transition_payload",
                "control": control,
                "phase4": phase4,
                "readiness": readiness,
            }
        try:
            marked_ready = manager.mark_v2_ready(**payload)
        except Exception as exc:
            return {
                "ok": False,
                "status": "BLOCKED",
                "code": _error_code(exc, "v2_ready_transition_failed"),
                "control": control,
                "phase4": phase4,
                "readiness": readiness,
                "error": str(exc),
            }
        readiness = dict(readiness)
        readiness["manifest_state"] = marked_ready.state.value
        readiness["generation"] = marked_ready.generation
    return {
        "ok": ready,
        "status": "PASS" if ready else "BLOCKED",
        "code": "" if ready else "v2_readiness_verification_failed",
        "control": control,
        "phase4": phase4,
        "readiness": readiness,
        "blockers": blockers,
    }


def _project_gui_control_outbox(workspace: Path) -> dict[str, Any]:
    """Advance the system projection checkpoint for committed GUI receipts."""

    store = SystemControlStore(workspace, write=False)
    with store.connection(write=True) as conn:
        with transaction(conn):
            row = conn.execute("SELECT MAX(sequence) FROM group_outbox").fetchone()
            maximum = 0 if row is None or row[0] is None else int(row[0])
            if maximum:
                conn.execute(
                    "UPDATE outbox_checkpoints SET last_sequence=?,updated_at=? "
                    "WHERE domain='system' AND last_sequence<?",
                    (maximum, "upgrade", maximum),
                )
    return {"status": "PASS", "max_sequence": maximum}


def run_upgrade(
    workspace: str | Path = ".",
    *,
    data_home: str | Path | None = None,
    apply: bool = False,
    confirm: str | None = None,
    migration_id: str | None = None,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    """Run the public upgrade flow and return a stable JSON-compatible report."""

    root = Path(workspace).expanduser().resolve()
    resolved_data_home = (
        Path(data_home).expanduser().resolve()
        if data_home is not None
        else resolve_data_home()
    )
    stages = _stages()
    manager = ManifestManager(root)

    try:
        current = manager.current(immutable=not apply)
    except Exception as exc:
        unknown = type("UnknownState", (), {"state": "UNKNOWN", "generation": None, "migration_id": ""})()
        stages["preflight"] = _stage(
            "BLOCKED", code=_error_code(exc, "v2_manifest_unavailable"), detail={"error": str(exc)}
        )
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=apply,
            current=unknown,
            stages=stages,
            status="BLOCKED",
            ok=False,
            stage="preflight",
            code=_error_code(exc, "v2_manifest_unavailable"),
            next_step=_next_step("error"),
            detail={"error": str(exc)},
        )

    state = current.state
    if state is ManifestState.V2_ACTIVE:
        stages["preflight"] = _stage(
            "PASS", ok=True, code="already_active", detail=_manifest_summary(manager, current)
        )
        stages["activate"] = _stage(
            "IDEMPOTENT", ok=True, code="already_active", detail=_manifest_summary(manager, current)
        )
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=apply,
            current=current,
            stages=stages,
            status=ManifestState.V2_ACTIVE.value,
            ok=True,
            stage="complete",
            code="already_active",
            next_step=_next_step("active"),
            activation_required=False,
        )

    if confirm is not None and not apply:
        stages["preflight"] = _stage(
            "BLOCKED", code="confirmation_requires_apply", detail={"confirm_supplied": True}
        )
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=False,
            current=current,
            stages=stages,
            status="BLOCKED",
            ok=False,
            stage="preflight",
            code="confirmation_requires_apply",
            next_step=_next_step("preview"),
        )
    if expected_generation is not None and current.generation != expected_generation:
        stages["preflight"] = _stage(
            "BLOCKED",
            code="manifest_generation_conflict",
            detail={"expected_generation": expected_generation, "current_generation": current.generation},
        )
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=apply,
            current=current,
            stages=stages,
            status="BLOCKED",
            ok=False,
            stage="preflight",
            code="manifest_generation_conflict",
            next_step=_next_step("error"),
        )
    if confirm is not None and confirm != CONFIRM_ACTIVE:
        stages["preflight"] = _stage(
            "BLOCKED",
            code="activation_confirmation_mismatch",
            detail={"required": CONFIRM_ACTIVE},
        )
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=apply,
            current=current,
            stages=stages,
            status="BLOCKED",
            ok=False,
            stage="preflight",
            code="activation_confirmation_mismatch",
            next_step=_next_step("confirmation"),
        )

    # The preflight is deliberately the only work done by the default path.
    try:
        control_preview = inspect_legacy_gui_control(root)
        if state is ManifestState.V2_READY:
            prepare_preview: dict[str, Any] = {
                "status": "NOT_REQUIRED",
                "ok": True,
                "reason": "v2_ready_can_resume",
                "writes_performed": False,
            }
        else:
            prepare_preview = prepare_v2_ready(
                root,
                apply=False,
                data_home=resolved_data_home,
                migration_id=migration_id,
                expected_generation=expected_generation,
            )
        stages["preflight"] = _stage(
            "PASS",
            ok=True,
            detail={
                "manifest": _manifest_summary(manager, current),
                "prepare": prepare_preview,
                "gui_control": control_preview,
            },
        )
    except Exception as exc:
        code = _error_code(exc, "upgrade_preflight_failed")
        stages["preflight"] = _stage("BLOCKED", code=code, detail={"error": str(exc)})
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=apply,
            current=current,
            stages=stages,
            status="BLOCKED",
            ok=False,
            stage="preflight",
            code=code,
            next_step=_next_step("error"),
            detail={"error": str(exc)},
        )

    if not apply:
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=False,
            current=current,
            stages=stages,
            status="PREVIEW",
            ok=True,
            stage="preflight",
            code="preview",
            next_step=_next_step("preview"),
            activation_required=state is ManifestState.V2_READY,
        )

    # From here on all writes are inside the explicit --apply path.
    working = current
    if working.state in {ManifestState.V1_ACTIVE, ManifestState.V2_BUILDING}:
        try:
            prepared = prepare_v2_workspace(
                root,
                apply=True,
                data_home=resolved_data_home,
                migration_id=migration_id,
                expected_generation=expected_generation,
            )
        except Exception as exc:
            code = _error_code(exc, "prepare_failed")
            stages["prepare"] = _stage("BLOCKED", writes_performed=True, code=code, detail={"error": str(exc)})
            current = manager.current()
            return _envelope(
                workspace=root,
                data_home=resolved_data_home,
                apply=True,
                current=current,
                stages=stages,
                status="BLOCKED",
                ok=False,
                stage="prepare",
                code=code,
                next_step=_next_step("resume"),
                writes_performed=True,
                detail={"error": str(exc)},
            )
        current = manager.current()
        if prepared.get("status") != ManifestState.V2_BUILDING.value or prepared.get("ok") is not True:
            code = "prepare_failed"
            stages["prepare"] = _stage("BLOCKED", writes_performed=True, code=code, detail=prepared)
            return _envelope(
                workspace=root,
                data_home=resolved_data_home,
                apply=True,
                current=current,
                stages=stages,
                status="BLOCKED",
                ok=False,
                stage="prepare",
                code=code,
                next_step=_next_step("resume"),
                writes_performed=True,
                detail=prepared,
            )
        stages["prepare"] = _stage("PASS", ok=True, writes_performed=True, detail=prepared)
        working = current
    else:
        stages["prepare"] = _stage(
            "SKIPPED", ok=True, code="already_v2_ready", detail=_manifest_summary(manager, working)
        )

    if working.state not in {ManifestState.V2_BUILDING, ManifestState.V2_READY}:
        code = "prepare_did_not_reach_v2_ready"
        stages["prepare"] = _stage("BLOCKED", writes_performed=True, code=code, detail=_manifest_summary(manager, working))
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=True,
            current=working,
            stages=stages,
            status="BLOCKED",
            ok=False,
            stage="prepare",
            code=code,
            next_step=_next_step("resume"),
            writes_performed=True,
        )

    try:
        control_result = migrate_legacy_gui_control(root)
        if control_result.get("ok") is not True:
            raise GuiControlMigrationError("gui_control_migration_failed")
        control_result["projection"] = _project_gui_control_outbox(root)
        stages["gui_control"] = _stage(
            "PASS", ok=True, writes_performed=True, detail=control_result
        )
    except Exception as exc:
        code = _error_code(exc, "gui_control_migration_failed")
        stages["gui_control"] = _stage("BLOCKED", writes_performed=True, code=code, detail={"error": str(exc)})
        current = manager.current()
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=True,
            current=current,
            stages=stages,
            status="BLOCKED",
            ok=False,
            stage="gui_control",
            code=code,
            next_step=_next_step("resume"),
            writes_performed=True,
            activation_required=current.state is ManifestState.V2_READY,
            detail={"error": str(exc)},
        )

    verified = _verify_ready(root, resolved_data_home, manager, control_preview)
    if verified.get("ok") is not True:
        code = str(verified.get("code") or "v2_readiness_verification_failed")
        stages["verify"] = _stage("BLOCKED", writes_performed=True, code=code, detail=verified)
        current = manager.current()
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=True,
            current=current,
            stages=stages,
            status="BLOCKED",
            ok=False,
            stage="verify",
            code=code,
            next_step=_next_step("resume"),
            writes_performed=True,
            activation_required=current.state is ManifestState.V2_READY,
            detail=verified,
        )
    stages["verify"] = _stage("PASS", ok=True, writes_performed=True, detail=verified)

    ready = manager.current()
    if confirm is None:
        stages["activate"] = _stage(
            "PENDING_CONFIRMATION",
            ok=True,
            writes_performed=False,
            code="activation_confirmation_required",
            detail={"required": CONFIRM_ACTIVE, "generation": ready.generation},
        )
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=True,
            current=ready,
            stages=stages,
            status=ManifestState.V2_READY.value,
            ok=True,
            stage="activate",
            code="activation_confirmation_required",
            next_step=_next_step("ready"),
            writes_performed=True,
            activation_required=True,
            detail={"required": CONFIRM_ACTIVE, "generation": ready.generation},
        )

    try:
        active = manager.activate_v2(expected_generation=ready.generation)
    except Exception as exc:
        code = _error_code(exc, "activation_failed")
        stages["activate"] = _stage("BLOCKED", code=code, detail={"error": str(exc)})
        current = manager.current()
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=True,
            current=current,
            stages=stages,
            status="BLOCKED",
            ok=False,
            stage="activate",
            code=code,
            next_step=_next_step("resume"),
            writes_performed=True,
            activation_required=current.state is ManifestState.V2_READY,
            detail={"error": str(exc)},
        )

    stages["activate"] = _stage(
        "PASS", ok=True, writes_performed=True, code="activated", detail=_manifest_summary(manager, active)
    )
    return _envelope(
        workspace=root,
        data_home=resolved_data_home,
        apply=True,
        current=active,
        stages=stages,
        status=ManifestState.V2_ACTIVE.value,
        ok=True,
        stage="complete",
        code="activated",
        next_step=_next_step("active"),
        writes_performed=True,
        activation_required=False,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memoryguard upgrade",
        description="Public V1 (0.6.2) to V2 upgrade orchestration.",
    )
    parser.add_argument("workspace_arg", nargs="?", help="workspace path (default: .)")
    parser.add_argument("-w", "--workspace", default="", help="workspace path")
    parser.add_argument("--data-home", help="explicit V1 global data home")
    parser.add_argument("--apply", action="store_true", help="write the resumable migration")
    parser.add_argument(
        "--confirm",
        metavar="V2_ACTIVE",
        help="activate only when exactly V2_ACTIVE is supplied",
    )
    parser.add_argument("--migration-id")
    parser.add_argument("--expected-generation", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.workspace and args.workspace_arg:
        payload = _envelope(
            workspace=Path(args.workspace).expanduser().resolve(),
            data_home=(Path(args.data_home).expanduser().resolve() if args.data_home else resolve_data_home()),
            apply=bool(args.apply),
            current=type("UnknownState", (), {"state": "UNKNOWN", "generation": None, "migration_id": ""})(),
            stages={name: _stage() for name in _STAGE_NAMES},
            status="BLOCKED",
            ok=False,
            stage="preflight",
            code="workspace_specified_twice",
            next_step="specify workspace once, using either the positional argument or --workspace",
        )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 2
    report = run_upgrade(
        args.workspace_arg or args.workspace or ".",
        data_home=args.data_home,
        apply=bool(args.apply),
        confirm=args.confirm,
        migration_id=args.migration_id,
        expected_generation=args.expected_generation,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str))
    return 0 if report.get("ok") is True else 2


__all__ = ["CONFIRM_ACTIVE", "SCHEMA", "build_parser", "main", "run_upgrade"]
