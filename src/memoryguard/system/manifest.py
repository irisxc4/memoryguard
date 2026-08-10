"""V2 activation manifest and its deliberately narrow state machine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping
from uuid import uuid4

from ..storage.database import connect_database, open_database
from ..storage.layout import LayoutError, WorkspaceV2Layout
from ..storage.schema import SchemaError, _apply_schema, _validate_schema_file
from ..storage.transaction import transaction


class ManifestError(RuntimeError):
    """A manifest could not be read or a state transition was invalid."""


class ManifestState(str, Enum):
    V1_ACTIVE = "V1_ACTIVE"
    V2_BUILDING = "V2_BUILDING"
    V2_READY = "V2_READY"
    V2_ACTIVE = "V2_ACTIVE"


_POINTER_FIELDS = (
    "workspace_source_pointer",
    "global_source_pointer",
    "data_home_root",
)

# Process-local proof that activation re-verified live V1 against the frozen
# Phase-2 source set. A JSON/RPC caller cannot manufacture this identity token.
_READINESS_VERIFICATION_ISSUER = object()
_ACTIVATION_VERIFICATION_ISSUER = object()


@dataclass(frozen=True, slots=True)
class _ReadinessVerification:
    migration_id: str
    snapshot_digest: str
    checked: int
    _issuer: object


@dataclass(frozen=True, slots=True)
class _ActivationVerification:
    migration_id: str
    snapshot_digest: str
    checked: int
    _issuer: object


@dataclass(frozen=True)
class ManifestRecord:
    manifest_id: str
    state: ManifestState
    generation: int
    migration_id: str
    source_digest: str
    target_digest: str
    manifest_digest: str
    digests: dict[str, Any]
    errors: dict[str, Any]
    last_error: str
    workspace_source_pointer: str
    global_source_pointer: str
    data_home_root: str
    checkpoints: dict[str, Any]
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row | Mapping[str, Any]) -> "ManifestRecord":
        def value(key: str, default: Any = "") -> Any:
            try:
                return row[key]  # type: ignore[index]
            except (KeyError, IndexError):
                return default

        def json_object(raw: Any, field: str) -> dict[str, Any]:
            if raw is None:
                raise ManifestError(f"manifest field {field} is NULL")
            try:
                parsed = json.loads(raw.decode() if isinstance(raw, bytes) else str(raw))
            except (TypeError, ValueError, UnicodeError) as exc:
                raise ManifestError(f"manifest field {field} contains invalid JSON") from exc
            if not isinstance(parsed, dict):
                raise ManifestError(f"manifest field {field} must contain a JSON object")
            return parsed

        def pointer_value(field: str) -> str:
            raw = value(field)
            if raw is None:
                raise ManifestError(f"manifest pointer {field} is NULL")
            text = raw.decode() if isinstance(raw, bytes) else str(raw)
            # New writers persist ordinary normalized path strings.  Accept a
            # JSON string for compatibility with early adapters, but reject a
            # malformed JSON-looking value rather than silently treating it as
            # an empty pointer.
            stripped = text.strip()
            if stripped[:1] in {"{", "[", '"'} or stripped in {"null", "true", "false"}:
                try:
                    parsed = json.loads(stripped)
                except (TypeError, ValueError, UnicodeError) as exc:
                    raise ManifestError(f"manifest pointer {field} contains invalid JSON") from exc
                if not isinstance(parsed, str):
                    raise ManifestError(f"manifest pointer {field} must be a JSON string")
                text = parsed
            if "\x00" in text:
                raise ManifestError(f"manifest pointer {field} contains NUL")
            if text and text != "NOT_CONFIGURED" and not Path(text).expanduser().is_absolute():
                raise ManifestError(f"manifest pointer {field} must be absolute")
            return text

        try:
            state = ManifestState(str(value("state")))
        except ValueError as exc:
            raise ManifestError("manifest contains unknown state") from exc
        raw_generation = value("generation", 0)
        if isinstance(raw_generation, bool) or type(raw_generation) is not int or raw_generation < 0:
            raise ManifestError("manifest generation is invalid")
        generation = raw_generation
        return cls(
            manifest_id=str(value("manifest_id")),
            state=state,
            generation=generation,
            migration_id=str(value("migration_id")),
            source_digest=str(value("source_digest")),
            target_digest=str(value("target_digest")),
            manifest_digest=str(value("manifest_digest")),
            digests=json_object(value("digests_json"), "digests_json"),
            errors=json_object(value("errors_json"), "errors_json"),
            last_error=str(value("last_error")),
            workspace_source_pointer=pointer_value("workspace_source_pointer"),
            global_source_pointer=pointer_value("global_source_pointer"),
            data_home_root=pointer_value("data_home_root"),
            checkpoints=json_object(value("checkpoints_json"), "checkpoints_json"),
            created_at=str(value("created_at")),
            updated_at=str(value("updated_at")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "state": self.state.value,
            "generation": self.generation,
            "migration_id": self.migration_id,
            "source_digest": self.source_digest,
            "target_digest": self.target_digest,
            "manifest_digest": self.manifest_digest,
            "digests": dict(self.digests),
            "errors": dict(self.errors),
            "last_error": self.last_error,
            "workspace_source_pointer": self.workspace_source_pointer,
            "global_source_pointer": self.global_source_pointer,
            "data_home_root": self.data_home_root,
            "checkpoints": dict(self.checkpoints),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


_TRANSITIONS: dict[ManifestState, frozenset[ManifestState]] = {
    ManifestState.V1_ACTIVE: frozenset({ManifestState.V2_BUILDING}),
    ManifestState.V2_BUILDING: frozenset({ManifestState.V2_READY, ManifestState.V1_ACTIVE}),
    ManifestState.V2_READY: frozenset({ManifestState.V2_ACTIVE, ManifestState.V1_ACTIVE}),
    ManifestState.V2_ACTIVE: frozenset({ManifestState.V1_ACTIVE}),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ManifestManager:
    """Read and atomically advance the workspace V2 activation pointer."""

    def __init__(self, workspace_or_layout: str | Path | WorkspaceV2Layout) -> None:
        if isinstance(workspace_or_layout, WorkspaceV2Layout):
            self.layout = workspace_or_layout
        else:
            self.layout = WorkspaceV2Layout(Path(workspace_or_layout))
        self.db_path = self.layout.manifest_db

    def exists(self) -> bool:
        self.layout.assert_database_path(self.db_path, "system")
        return self.db_path.is_file()

    def _prepare_write(self) -> None:
        """Make the fixed system path safe before opening SQLite writable."""

        try:
            self.layout.ensure_dirs()
            self.layout.assert_database_path(self.db_path, "system")
            _validate_schema_file(self.db_path, "system")
        except (LayoutError, SchemaError) as exc:
            raise ManifestError(f"unsafe or unsupported system manifest path: {exc}") from exc

    @property
    def state(self) -> ManifestState:
        return self.current().state

    @property
    def current_state(self) -> ManifestState:
        return self.state

    @property
    def status(self) -> ManifestState:
        return self.state

    @staticmethod
    def _default_record() -> ManifestRecord:
        now = _now()
        return ManifestRecord(
            manifest_id="workspace",
            state=ManifestState.V1_ACTIVE,
            generation=0,
            migration_id="",
            source_digest="",
            target_digest="",
            manifest_digest="",
            digests={},
            errors={},
            last_error="",
            workspace_source_pointer="",
            global_source_pointer="",
            data_home_root="",
            checkpoints={},
            created_at=now,
            updated_at=now,
        )

    def current(self, *, immutable: bool = False) -> ManifestRecord:
        """Return the persisted pointer, without creating a database."""

        try:
            self.layout.assert_database_path(self.db_path, "system")
            _validate_schema_file(self.db_path, "system", immutable=immutable)
        except (LayoutError, SchemaError) as exc:
            raise ManifestError(f"cannot safely inspect system manifest: {exc}") from exc
        if not self.db_path.exists():
            return self._default_record()
        if not self.db_path.is_file():
            raise ManifestError("system manifest path is not a regular file")
        try:
            with open_database(self.db_path, readonly=True, immutable=immutable) as conn:
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                # A failed first write may leave an empty SQLite file after
                # its DDL transaction rolls back.  Once a database file exists,
                # however, an empty/partial schema is evidence of corruption;
                # only a genuinely absent file may use the V1 default.
                if "manifest" not in tables:
                    raise ManifestError("system manifest table is missing")
                row = conn.execute(
                    "SELECT * FROM manifest WHERE manifest_id='workspace'"
                ).fetchone()
        except (sqlite3.Error, OSError) as exc:
            raise ManifestError(f"cannot read system manifest: {exc}") from exc
        if row is None:
            raise ManifestError("system manifest row is missing")
        record = ManifestRecord.from_row(row)
        normalised = self._normalise_pointer_tuple(
            {field: getattr(record, field) for field in _POINTER_FIELDS}
        )
        if any(
            getattr(record, field) != normalised[field] for field in _POINTER_FIELDS
        ):
            raise ManifestError("system manifest source pointers are not normalized")
        return record

    def read(self) -> ManifestRecord:
        return self.current()

    @staticmethod
    def _pointer_text(value: str | Path | None, field: str) -> str | None:
        if value is None:
            return None
        text = str(value)
        if "\x00" in text:
            raise ManifestError(f"source pointer {field} contains NUL")
        if not text.strip():
            raise ManifestError(f"source pointer {field} must not be empty")
        return text

    def _normalise_pointer_tuple(
        self,
        values: Mapping[str, str],
        *,
        allow_empty: bool = True,
    ) -> dict[str, str]:
        """Validate and canonicalise the persisted V1 source identity."""

        workspace_raw = str(values.get("workspace_source_pointer", ""))
        global_raw = str(values.get("global_source_pointer", ""))
        data_raw = str(values.get("data_home_root", ""))
        if not any((workspace_raw, global_raw, data_raw)):
            if allow_empty:
                return {field: "" for field in _POINTER_FIELDS}
            raise ManifestError("source pointers must be explicit")
        if workspace_raw in {"", "NOT_CONFIGURED"}:
            raise ManifestError("workspace_source_pointer must be an absolute workspace path")
        workspace_path = Path(workspace_raw).expanduser()
        if not workspace_path.is_absolute():
            raise ManifestError("workspace_source_pointer must be absolute")
        try:
            workspace_resolved = workspace_path.resolve(strict=False)
        except OSError as exc:
            raise ManifestError("workspace_source_pointer cannot be resolved") from exc
        expected_workspace = self.layout.workspace.resolve(strict=False)
        if workspace_resolved != expected_workspace:
            raise ManifestError(
                "workspace_source_pointer must resolve to the manifest workspace"
            )

        configured = (global_raw, data_raw)
        if configured == ("NOT_CONFIGURED", "NOT_CONFIGURED"):
            return {
                "workspace_source_pointer": str(expected_workspace),
                "global_source_pointer": "NOT_CONFIGURED",
                "data_home_root": "NOT_CONFIGURED",
            }
        if "NOT_CONFIGURED" in configured or not all(configured):
            raise ManifestError(
                "global_source_pointer and data_home_root must both be configured or both be NOT_CONFIGURED"
            )
        global_path = Path(global_raw).expanduser()
        data_path = Path(data_raw).expanduser()
        if not global_path.is_absolute() or not data_path.is_absolute():
            raise ManifestError("global source pointers must be absolute")
        try:
            global_resolved = global_path.resolve(strict=False)
            data_resolved = data_path.resolve(strict=False)
            global_resolved.relative_to(data_resolved)
        except (OSError, ValueError) as exc:
            raise ManifestError(
                "global_source_pointer must resolve to data_home_root or a child"
            ) from exc
        return {
            "workspace_source_pointer": str(expected_workspace),
            "global_source_pointer": str(global_resolved),
            "data_home_root": str(data_resolved),
        }

    def _merge_pointers(
        self,
        current: ManifestRecord,
        *,
        workspace_source_pointer: str | Path | None,
        global_source_pointer: str | Path | None,
        data_home_root: str | Path | None,
    ) -> dict[str, str]:
        supplied = {
            "workspace_source_pointer": self._pointer_text(
                workspace_source_pointer, "workspace_source_pointer"
            ),
            "global_source_pointer": self._pointer_text(
                global_source_pointer, "global_source_pointer"
            ),
            "data_home_root": self._pointer_text(data_home_root, "data_home_root"),
        }
        result: dict[str, str] = {}
        for field in _POINTER_FIELDS:
            prior = str(getattr(current, field))
            requested = supplied[field]
            result[field] = prior if requested is None else requested
        # If a caller starts declaring pointers, require the complete tuple
        # when no prior tuple exists.  This prevents a later implicit default
        # from being mistaken for an explicit DataHome identity.
        if any(item is not None for item in supplied.values()) and not any(
            str(getattr(current, field)) for field in _POINTER_FIELDS
        ):
            missing = [field for field, item in supplied.items() if item is None]
            if missing:
                raise ManifestError(
                    "source pointers must be supplied together: " + ", ".join(missing)
                )
        normalised = self._normalise_pointer_tuple(result)
        for field in _POINTER_FIELDS:
            prior = str(getattr(current, field))
            if prior and normalised[field] != prior:
                raise ManifestError(f"source pointer is immutable: {field}")
        return normalised

    def transition(
        self,
        target: ManifestState | str,
        *,
        migration_id: str = "",
        source_digest: str = "",
        target_digest: str = "",
        manifest_digest: str = "",
        digests: Mapping[str, Any] | None = None,
        error: str | None = None,
        errors: Mapping[str, Any] | None = None,
        expected_generation: int | None = None,
        workspace_source_pointer: str | Path | None = None,
        global_source_pointer: str | Path | None = None,
        data_home_root: str | Path | None = None,
        readiness_verification: Any = None,
        activation_verification: Any = None,
    ) -> ManifestRecord:
        try:
            next_state = target if isinstance(target, ManifestState) else ManifestState(str(target))
        except ValueError as exc:
            raise ManifestError(f"unknown manifest state: {target!r}") from exc
        requested_migration_id = migration_id
        if readiness_verification is not None and next_state is not ManifestState.V2_READY:
            raise ManifestError("readiness verification is only valid for V2_READY")
        if activation_verification is not None and next_state is not ManifestState.V2_ACTIVE:
            raise ManifestError("activation verification is only valid for V2_ACTIVE")
        error_map = dict(errors or {})
        if error:
            error_map.setdefault("message", error)
        last_error = str(error or error_map.get("message", ""))
        now = _now()
        self._prepare_write()
        conn = connect_database(self.db_path, readonly=False)
        try:
            with transaction(conn):
                # Schema creation is inside this transaction, so a failed
                # bootstrap cannot leave a half-created manifest database.
                _apply_schema(conn, "system", path=self.db_path, now=now)
                row = conn.execute(
                    "SELECT * FROM manifest WHERE manifest_id='workspace'"
                ).fetchone()
                if row is None:
                    raise ManifestError("system manifest row is missing")
                current = ManifestRecord.from_row(row)
                if expected_generation is not None:
                    if isinstance(expected_generation, bool) or type(expected_generation) is not int or expected_generation < 0:
                        raise ManifestError("manifest expected_generation is invalid")
                    if current.generation != expected_generation:
                        raise ManifestError("manifest generation conflict")
                pointer_values = self._merge_pointers(
                    current,
                    workspace_source_pointer=workspace_source_pointer,
                    global_source_pointer=global_source_pointer,
                    data_home_root=data_home_root,
                )
                # Retrying an already-applied transition is idempotent.  Do
                # not append a ledger row or advance generation, but reject a
                # replay that attempts to mutate immutable migration evidence.
                if next_state is current.state:
                    if requested_migration_id and requested_migration_id != current.migration_id:
                        raise ManifestError("idempotent manifest replay has a different migration_id")
                    supplied_digests = {
                        key: value
                        for key, value in {
                            "source_digest": source_digest,
                            "target_digest": target_digest,
                            "manifest_digest": manifest_digest,
                        }.items()
                        if value
                    }
                    current_digests = {
                        "source_digest": current.source_digest,
                        "target_digest": current.target_digest,
                        "manifest_digest": current.manifest_digest,
                    }
                    if any(current_digests[key] != value for key, value in supplied_digests.items()):
                        raise ManifestError("idempotent manifest replay has different digests")
                    if digests is not None and dict(digests) != current.digests:
                        raise ManifestError("idempotent manifest replay has different digest metadata")
                    if error and error != current.last_error:
                        raise ManifestError("idempotent manifest replay has different error")
                    if errors is not None and dict(errors) != current.errors:
                        raise ManifestError("idempotent manifest replay has different errors")
                    if any(
                        supplied is not None
                        and pointer_values[field] != getattr(current, field)
                        for field, supplied in (
                            ("workspace_source_pointer", workspace_source_pointer),
                            ("global_source_pointer", global_source_pointer),
                            ("data_home_root", data_home_root),
                        )
                    ):
                        raise ManifestError("idempotent manifest replay has different source pointers")
                    return current
                migration_id = requested_migration_id or uuid4().hex
                allowed = _TRANSITIONS[current.state]
                if next_state not in allowed:
                    raise ManifestError(
                        f"illegal manifest transition {current.state.value} -> {next_state.value}"
                    )
                if next_state is ManifestState.V1_ACTIVE and not last_error:
                    raise ManifestError("return to V1_ACTIVE requires failure details")
                if next_state is ManifestState.V2_BUILDING and current.state is not ManifestState.V1_ACTIVE:
                    raise ManifestError("V2_BUILDING may only start from V1_ACTIVE")
                if next_state is ManifestState.V2_BUILDING and current.state is ManifestState.V1_ACTIVE:
                    # A failed/rolled-back migration batch is never reopened.
                    # The same ID may carry BUILDING -> READY -> ACTIVE (or a
                    # failure event), but a new V1_ACTIVE -> BUILDING edge is
                    # always a new batch.
                    existing_batch = conn.execute(
                        "SELECT 1 FROM migration_ledger WHERE migration_id=? LIMIT 1",
                        (migration_id,),
                    ).fetchone()
                    if existing_batch is not None:
                        raise ManifestError(
                            "migration_id has already been used by a previous batch"
                        )
                if next_state is ManifestState.V1_ACTIVE:
                    # Rollback/failure keeps the prior migration evidence
                    # available for post-mortem audit instead of erasing it.
                    migration_id = requested_migration_id or current.migration_id
                    source_digest = source_digest or current.source_digest
                    target_digest = target_digest or current.target_digest
                    manifest_digest = manifest_digest or current.manifest_digest
                digest_map = dict(digests or {})
                if next_state is ManifestState.V1_ACTIVE:
                    inherited = dict(current.digests)
                    inherited.update(digest_map)
                    digest_map = inherited
                    if current.checkpoints and "checkpoints" not in digest_map:
                        digest_map["checkpoints"] = dict(current.checkpoints)
                    error_map.setdefault("reason", last_error)
                    error_map.setdefault("generation", current.generation)
                    error_map.setdefault("source_digest", source_digest)
                    error_map.setdefault("target_digest", target_digest)
                    error_map.setdefault("manifest_digest", manifest_digest)
                    error_map.setdefault("digests", dict(digest_map))
                    error_map.setdefault("checkpoints", dict(digest_map.get("checkpoints", current.checkpoints)))
                if next_state is ManifestState.V2_READY:
                    if "checkpoints" not in digest_map and "checkpoint" in digest_map:
                        digest_map["checkpoints"] = digest_map["checkpoint"]
                    missing = [
                        name
                        for name, value in {
                            "source_digest": source_digest,
                            "target_digest": target_digest,
                            "manifest_digest": manifest_digest,
                        }.items()
                        if not value
                    ]
                    if missing:
                        raise ManifestError(
                            "V2_READY requires immutable digests: " + ", ".join(missing)
                        )
                    if digest_map.get("validator_passed") is not True:
                        raise ManifestError("V2_READY requires validator_passed=true")
                    if not digest_map.get("checkpoints"):
                        raise ManifestError("V2_READY requires validation checkpoints")
                    # A production prepare records a frozen Phase-2 source set.
                    # READY may only freeze normal writes after the latest live
                    # V1 source verification proved that frozen set current.
                    phase2_sources = current.checkpoints.get("phase2_sources", {}) if isinstance(current.checkpoints, Mapping) else {}
                    source_snapshot = phase2_sources.get("snapshot", {}) if isinstance(phase2_sources, Mapping) else {}
                    if isinstance(source_snapshot, Mapping) and str(source_snapshot.get("mode") or "") == "frozen":
                        live_check = current.checkpoints.get("phase2_live_source_verification", {})
                        if not isinstance(live_check, Mapping) or str(live_check.get("status") or "") != "PASS":
                            raise ManifestError("V2_READY requires a passing live V1 source verification")
                        expected_snapshot_digest = str(live_check.get("snapshot_digest") or "")
                        if not expected_snapshot_digest:
                            raise ManifestError("V2_READY live source verification lacks snapshot digest")
                        if not isinstance(readiness_verification, _ReadinessVerification) or readiness_verification._issuer is not _READINESS_VERIFICATION_ISSUER:
                            raise ManifestError("V2_READY requires fresh process-issued live source verification")
                        if readiness_verification.migration_id != current.migration_id:
                            raise ManifestError("V2_READY live source verification migration mismatch")
                        if readiness_verification.snapshot_digest != expected_snapshot_digest:
                            raise ManifestError("V2_READY live source verification snapshot mismatch")
                        error_map["readiness_source_verification"] = {
                            "status": "PASS",
                            "checked": int(readiness_verification.checked),
                            "snapshot_digest": readiness_verification.snapshot_digest,
                            "migration_id": readiness_verification.migration_id,
                        }
                    prior_checkpoints = current.digests.get("checkpoints", {})
                    if prior_checkpoints:
                        if not isinstance(prior_checkpoints, Mapping) or not isinstance(
                            digest_map["checkpoints"], Mapping
                        ):
                            raise ManifestError("manifest checkpoints must be mappings")
                        conflicts = {
                            key
                            for key, value in prior_checkpoints.items()
                            if key in digest_map["checkpoints"]
                            and digest_map["checkpoints"][key] != value
                        }
                        if conflicts:
                            raise ManifestError("V2_READY cannot replace build checkpoints")
                        digest_map["checkpoints"] = {
                            **dict(prior_checkpoints),
                            **dict(digest_map["checkpoints"]),
                        }
                if next_state is ManifestState.V2_ACTIVE:
                    # Activation carries forward READY evidence; callers may
                    # omit it, but may never replace it.
                    if current.state is not ManifestState.V2_READY:
                        raise ManifestError("V2_ACTIVE requires a V2_READY manifest")
                    phase2_sources = current.checkpoints.get("phase2_sources", {}) if isinstance(current.checkpoints, Mapping) else {}
                    source_snapshot = phase2_sources.get("snapshot", {}) if isinstance(phase2_sources, Mapping) else {}
                    frozen_source = isinstance(source_snapshot, Mapping) and str(source_snapshot.get("mode") or "") == "frozen"
                    if frozen_source:
                        ready_check = current.checkpoints.get("phase2_live_source_verification", {})
                        if not isinstance(ready_check, Mapping) or str(ready_check.get("status") or "") != "PASS":
                            raise ManifestError("V2_ACTIVE requires READY live source verification evidence")
                        if not isinstance(activation_verification, _ActivationVerification) or activation_verification._issuer is not _ACTIVATION_VERIFICATION_ISSUER:
                            raise ManifestError("V2_ACTIVE requires fresh process-issued live source verification")
                        if activation_verification.migration_id != current.migration_id:
                            raise ManifestError("V2_ACTIVE live source verification migration mismatch")
                        expected_snapshot_digest = str(ready_check.get("snapshot_digest") or "")
                        if not expected_snapshot_digest or activation_verification.snapshot_digest != expected_snapshot_digest:
                            raise ManifestError("V2_ACTIVE live source verification snapshot mismatch")
                        error_map["activation_source_verification"] = {
                            "status": "PASS",
                            "checked": int(activation_verification.checked),
                            "snapshot_digest": activation_verification.snapshot_digest,
                            "migration_id": activation_verification.migration_id,
                        }
                    if requested_migration_id and requested_migration_id != current.migration_id:
                        raise ManifestError("V2_ACTIVE must inherit READY migration_id")
                    for name, supplied in {
                        "source_digest": source_digest,
                        "target_digest": target_digest,
                        "manifest_digest": manifest_digest,
                    }.items():
                        if supplied and supplied != getattr(current, name):
                            raise ManifestError("V2_ACTIVE cannot replace READY digests")
                    if digests is not None and dict(digests) != current.digests:
                        raise ManifestError("V2_ACTIVE cannot replace READY digest metadata")
                    migration_id = current.migration_id
                    source_digest = current.source_digest
                    target_digest = current.target_digest
                    manifest_digest = current.manifest_digest
                    digest_map = dict(current.digests)
                    pointer_values = {
                        field: getattr(current, field) for field in _POINTER_FIELDS
                    }
                generation = current.generation + 1
                transition_id = f"{migration_id}:{generation}:{next_state.value}"
                conn.execute(
                    "INSERT INTO migration_ledger(transition_id, migration_id, from_state, to_state, generation, "
                    "source_digest, target_digest, status, error_json, started_at, completed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        transition_id,
                        migration_id,
                        current.state.value,
                        next_state.value,
                        generation,
                        source_digest,
                        target_digest,
                        "failed" if next_state is ManifestState.V1_ACTIVE else "completed",
                        json.dumps(error_map, ensure_ascii=False, sort_keys=True),
                        now,
                        now,
                    ),
                )
                update = conn.execute(
                    "UPDATE manifest SET state=?, generation=?, migration_id=?, source_digest=?, "
                    "target_digest=?, manifest_digest=?, digests_json=?, errors_json=?, "
                    "last_error=?, workspace_source_pointer=?, global_source_pointer=?, "
                    "data_home_root=?, checkpoints_json=?, updated_at=? WHERE manifest_id='workspace'"
                    + (" AND generation=?" if expected_generation is not None else ""),
                    (
                        next_state.value,
                        generation,
                        migration_id,
                        source_digest,
                        target_digest,
                        manifest_digest,
                        json.dumps(digest_map, ensure_ascii=False, sort_keys=True),
                        json.dumps(error_map, ensure_ascii=False, sort_keys=True),
                        last_error,
                        pointer_values["workspace_source_pointer"],
                        pointer_values["global_source_pointer"],
                        pointer_values["data_home_root"],
                        json.dumps(digest_map.get("checkpoints", {}), ensure_ascii=False, sort_keys=True),
                        now,
                    ) + ((expected_generation,) if expected_generation is not None else ()),
                )
                if update.rowcount != 1:
                    raise ManifestError("manifest generation conflict")
                result_row = conn.execute(
                    "SELECT * FROM manifest WHERE manifest_id='workspace'"
                ).fetchone()
                assert result_row is not None
                result = ManifestRecord.from_row(result_row)
            return result
        except ManifestError:
            raise
        except sqlite3.IntegrityError as exc:
            raise ManifestError(f"manifest transition could not be persisted: {exc}") from exc
        finally:
            conn.close()

    def begin(
        self,
        *,
        migration_id: str = "",
        source_digest: str = "",
        target_digest: str = "",
        manifest_digest: str = "",
        digests: Mapping[str, Any] | None = None,
        workspace_source_pointer: str | Path | None = None,
        global_source_pointer: str | Path | None = None,
        data_home_root: str | Path | None = None,
    ) -> ManifestRecord:
        """Start a new V2 build batch with explicit source pointers."""

        return self.transition(
            ManifestState.V2_BUILDING,
            migration_id=migration_id,
            source_digest=source_digest,
            target_digest=target_digest,
            manifest_digest=manifest_digest,
            digests=digests,
            workspace_source_pointer=workspace_source_pointer,
            global_source_pointer=global_source_pointer,
            data_home_root=data_home_root,
        )

    build = begin

    def set_source_pointers(
        self,
        *,
        workspace_source_pointer: str | Path,
        global_source_pointer: str | Path,
        data_home_root: str | Path,
    ) -> ManifestRecord:
        """Persist the explicit source identity before a build starts.

        This is idempotent for the same tuple and rejects replacement once a
        tuple has been recorded or a build has advanced beyond ``V1_ACTIVE``.
        """

        requested = {
            "workspace_source_pointer": self._pointer_text(
                workspace_source_pointer, "workspace_source_pointer"
            ),
            "global_source_pointer": self._pointer_text(
                global_source_pointer, "global_source_pointer"
            ),
            "data_home_root": self._pointer_text(data_home_root, "data_home_root"),
        }
        assert all(value is not None for value in requested.values())
        self._prepare_write()
        conn = connect_database(self.db_path, readonly=False)
        try:
            with transaction(conn):
                _apply_schema(conn, "system", path=self.db_path, now=_now())
                row = conn.execute(
                    "SELECT * FROM manifest WHERE manifest_id='workspace'"
                ).fetchone()
                if row is None:
                    raise ManifestError("system manifest row is missing")
                current = ManifestRecord.from_row(row)
                if current.state is not ManifestState.V1_ACTIVE:
                    raise ManifestError("source pointers can only be set before a V2 build")
                merged = self._merge_pointers(
                    current,
                    workspace_source_pointer=requested["workspace_source_pointer"],
                    global_source_pointer=requested["global_source_pointer"],
                    data_home_root=requested["data_home_root"],
                )
                now = _now()
                conn.execute(
                    "UPDATE manifest SET workspace_source_pointer=?, "
                    "global_source_pointer=?, data_home_root=?, updated_at=? "
                    "WHERE manifest_id='workspace'",
                    (
                        merged["workspace_source_pointer"],
                        merged["global_source_pointer"],
                        merged["data_home_root"],
                        now,
                    ),
                )
                updated = conn.execute(
                    "SELECT * FROM manifest WHERE manifest_id='workspace'"
                ).fetchone()
                assert updated is not None
                return ManifestRecord.from_row(updated)
        except sqlite3.IntegrityError as exc:
            raise ManifestError(f"source pointers could not be persisted: {exc}") from exc
        finally:
            conn.close()

    def record_checkpoint(
        self,
        checkpoint: Mapping[str, Any],
        *,
        migration_id: str = "",
    ) -> ManifestRecord:
        """Persist a migration checkpoint without changing manifest state.

        Checkpoints are monotonic evidence within one migration batch.  A
        repeated identical checkpoint is idempotent; replacing an existing key
        or writing a checkpoint for another migration is rejected.
        """

        if not isinstance(checkpoint, Mapping) or not checkpoint:
            raise ManifestError("checkpoint must be a non-empty mapping")
        now = _now()
        self._prepare_write()
        conn = connect_database(self.db_path, readonly=False)
        try:
            with transaction(conn):
                _apply_schema(conn, "system", path=self.db_path, now=now)
                row = conn.execute(
                    "SELECT * FROM manifest WHERE manifest_id='workspace'"
                ).fetchone()
                if row is None:
                    raise ManifestError("system manifest row is missing")
                current = ManifestRecord.from_row(row)
                if current.state not in {ManifestState.V2_BUILDING, ManifestState.V2_READY}:
                    raise ManifestError("checkpoints are only writable while V2 builds or validates")
                if migration_id and migration_id != current.migration_id:
                    raise ManifestError("checkpoint migration_id does not match manifest")
                merged = dict(current.checkpoints)
                for key, value in checkpoint.items():
                    if key in merged and merged[key] != value:
                        raise ManifestError(f"checkpoint is immutable: {key}")
                    merged[str(key)] = value
                digests = dict(current.digests)
                existing_digest_checkpoints = digests.get("checkpoints")
                if existing_digest_checkpoints is not None:
                    if not isinstance(existing_digest_checkpoints, Mapping):
                        raise ManifestError("checkpoint digest metadata must be a mapping")
                    if any(
                        key in merged and merged[key] != value
                        for key, value in existing_digest_checkpoints.items()
                    ):
                        raise ManifestError("checkpoint conflicts with immutable digest metadata")
                digests["checkpoints"] = merged
                conn.execute(
                    "UPDATE manifest SET digests_json=?, checkpoints_json=?, updated_at=? "
                    "WHERE manifest_id='workspace'",
                    (
                        json.dumps(digests, ensure_ascii=False, sort_keys=True),
                        json.dumps(merged, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                updated = conn.execute(
                    "SELECT * FROM manifest WHERE manifest_id='workspace'"
                ).fetchone()
                assert updated is not None
                return ManifestRecord.from_row(updated)
        finally:
            conn.close()

    def record_checkpoint_attempt(
        self,
        checkpoint: Mapping[str, Any],
        *,
        migration_id: str = "",
        expected_generation: int | None = None,
        status: str = "",
    ) -> ManifestRecord:
        """Append resumable checkpoint attempt while preserving prior evidence.

        ``checkpoints[domain]`` is the latest authoritative result.  Previous
        values remain under ``_history`` and every changed attempt is retained
        under ``_attempts`` with deterministic digest plus monotonic sequence.
        Replaying identical result is idempotent; state/generation never move.
        """

        if not isinstance(checkpoint, Mapping) or len(checkpoint) != 1:
            raise ManifestError("checkpoint attempt must contain one mapping entry")
        key, raw_value = next(iter(checkpoint.items()))
        key = str(key)
        value = dict(raw_value) if isinstance(raw_value, Mapping) else {"value": raw_value}
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        value_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        inferred = str(status or value.get("status") or "").strip().upper()
        if not inferred:
            inferred = "FAILED" if value.get("ok") is False or value.get("errors") else "UNKNOWN"
        now = _now()
        self._prepare_write()
        conn = connect_database(self.db_path, readonly=False)
        try:
            with transaction(conn):
                _apply_schema(conn, "system", path=self.db_path, now=now)
                row = conn.execute("SELECT * FROM manifest WHERE manifest_id='workspace'").fetchone()
                if row is None:
                    raise ManifestError("system manifest row is missing")
                current = ManifestRecord.from_row(row)
                if current.state not in {ManifestState.V2_BUILDING, ManifestState.V2_READY}:
                    raise ManifestError("checkpoint attempts are only writable while V2 builds or validates")
                if migration_id and migration_id != current.migration_id:
                    raise ManifestError("checkpoint migration_id does not match manifest")
                if expected_generation is not None and current.generation != expected_generation:
                    raise ManifestError("manifest generation conflict")
                checkpoints = dict(current.checkpoints)
                attempts = checkpoints.get("_attempts", [])
                history = checkpoints.get("_history", [])
                authoritative = checkpoints.get("_authoritative", {})
                if not isinstance(attempts, list) or not isinstance(history, list) or not isinstance(authoritative, Mapping):
                    raise ManifestError("manifest checkpoint attempt metadata is invalid")
                prior = checkpoints.get(key)
                # Same value replay is a no-op, preserving idempotent reruns.
                if prior == value:
                    return current
                # Successful reruns may legitimately report a different
                # idempotency label/count (for example MIGRATED -> IDEMPOTENT)
                # without representing corrected data.  Keep first successful
                # checkpoint authoritative; only failure/success boundary
                # changes create a new attempt and replace the current view.
                failure_states = {"FAILED", "ERROR", "BLOCKED", "UNKNOWN", "UNAVAILABLE", "NOT_EVALUATED"}
                prior_status = str(prior.get("status") or "").strip().upper() if isinstance(prior, Mapping) else ""
                if prior is not None and prior_status not in failure_states and inferred not in failure_states:
                    return current
                sequence = max((int(item.get("sequence", 0)) for item in attempts if isinstance(item, Mapping)), default=0) + 1
                attempt_id = f"{current.migration_id}:{key}:{sequence}:{value_digest[:16]}"
                attempts = [*attempts, {
                    "attempt_id": attempt_id,
                    "sequence": sequence,
                    "migration_id": current.migration_id,
                    "checkpoint": key,
                    "status": inferred,
                    "result_digest": value_digest,
                    "result": value,
                    "created_at": now,
                }]
                if prior is not None:
                    history = [*history, {
                        "checkpoint": key,
                        "result": prior,
                        "preserved_at": now,
                    }]
                checkpoints[key] = value
                checkpoints["_attempts"] = attempts
                checkpoints["_history"] = history
                authoritative = dict(authoritative)
                authoritative[key] = {
                    "attempt_id": attempt_id,
                    "sequence": sequence,
                    "status": inferred,
                    "result_digest": value_digest,
                }
                checkpoints["_authoritative"] = authoritative
                digests = dict(current.digests)
                digests["checkpoints"] = checkpoints
                conn.execute(
                    "UPDATE manifest SET digests_json=?,checkpoints_json=?,updated_at=? WHERE manifest_id='workspace' AND generation=?",
                    (json.dumps(digests, ensure_ascii=False, sort_keys=True), json.dumps(checkpoints, ensure_ascii=False, sort_keys=True), now, current.generation),
                )
                updated = conn.execute("SELECT * FROM manifest WHERE manifest_id='workspace'").fetchone()
                assert updated is not None
                return ManifestRecord.from_row(updated)
        finally:
            conn.close()

    checkpoint = record_checkpoint
    set_pointers = set_source_pointers

    def fail(
        self,
        *,
        error: str,
        migration_id: str = "",
        source_digest: str = "",
        target_digest: str = "",
        manifest_digest: str = "",
        digests: Mapping[str, Any] | None = None,
        expected_generation: int | None = None,
        errors: Mapping[str, Any] | None = None,
    ) -> ManifestRecord:
        """Record a failed migration and return to the V1 read source."""

        return self.transition(
            ManifestState.V1_ACTIVE,
            migration_id=migration_id,
            source_digest=source_digest,
            target_digest=target_digest,
            manifest_digest=manifest_digest,
            digests=digests,
            expected_generation=expected_generation,
            error=error,
            errors=errors,
        )

    def mark_v2_ready(self, **kwargs: Any) -> ManifestRecord:
        """Freeze normal writes only after a fresh live-V1 drift check."""

        if "readiness_verification" in kwargs:
            raise ManifestError("readiness verification is server-issued only")
        current = self.current()
        phase2_sources = current.checkpoints.get("phase2_sources", {}) if isinstance(current.checkpoints, Mapping) else {}
        snapshot = phase2_sources.get("snapshot", {}) if isinstance(phase2_sources, Mapping) else {}
        if current.state is ManifestState.V2_BUILDING and isinstance(snapshot, Mapping) and str(snapshot.get("mode") or "") == "frozen":
            try:
                from ..migration.workspace_prepare import verify_v2_source_snapshot

                result = verify_v2_source_snapshot(
                    self.layout.workspace,
                    migration_id=current.migration_id,
                )
            except Exception as exc:
                raise ManifestError("V2 readiness live source verification failed") from exc
            if result.get("activation_safe") is not True or str(result.get("status") or "") != "PASS":
                raise ManifestError("V2 readiness blocked by live V1 source drift")
            snapshot_digest = str(result.get("snapshot_digest") or "")
            if not snapshot_digest:
                raise ManifestError("V2 readiness live source verification lacks snapshot digest")
            kwargs["readiness_verification"] = _ReadinessVerification(
                migration_id=current.migration_id,
                snapshot_digest=snapshot_digest,
                checked=int(result.get("checked") or 0),
                _issuer=_READINESS_VERIFICATION_ISSUER,
            )
        return self.transition(ManifestState.V2_READY, **kwargs)

    ready_v2 = mark_v2_ready

    def activate_v2(self, **kwargs: Any) -> ManifestRecord:
        """Activate only after a fresh live-V1 drift check for frozen builds."""

        if "activation_verification" in kwargs:
            raise ManifestError("activation verification is server-issued only")
        current = self.current()
        phase2_sources = current.checkpoints.get("phase2_sources", {}) if isinstance(current.checkpoints, Mapping) else {}
        snapshot = phase2_sources.get("snapshot", {}) if isinstance(phase2_sources, Mapping) else {}
        if current.state is ManifestState.V2_READY and isinstance(snapshot, Mapping) and str(snapshot.get("mode") or "") == "frozen":
            try:
                from ..migration.workspace_prepare import verify_v2_source_snapshot

                result = verify_v2_source_snapshot(
                    self.layout.workspace,
                    migration_id=current.migration_id,
                )
            except Exception as exc:
                raise ManifestError("V2 activation live source verification failed") from exc
            if result.get("activation_safe") is not True or str(result.get("status") or "") != "PASS":
                raise ManifestError("V2 activation blocked by live V1 source drift")
            snapshot_digest = str(result.get("snapshot_digest") or "")
            if not snapshot_digest:
                raise ManifestError("V2 activation live source verification lacks snapshot digest")
            kwargs["activation_verification"] = _ActivationVerification(
                migration_id=current.migration_id,
                snapshot_digest=snapshot_digest,
                checked=int(result.get("checked") or 0),
                _issuer=_ACTIVATION_VERIFICATION_ISSUER,
            )
        return self.transition(ManifestState.V2_ACTIVE, **kwargs)


# Compatibility name used by the initial Phase-1 draft contract.
SystemManifestStore = ManifestManager
