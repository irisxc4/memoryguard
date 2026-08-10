"""Phase 2 shadow-build coordinator.

The coordinator composes the already isolated domain migrators.  It records
progress in the system manifest, drains cross-database evidence outboxes and
stops at ``V2_BUILDING``.  Runtime reads/writes are never switched here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid
from typing import Any, Callable, Mapping

from ..content import ContentStore
from ..evidence import EvidenceStore
from ..evidence.store import _MIGRATION_CAPABILITY as _EVIDENCE_MIGRATION_CAPABILITY
from ..memory import MemoryAtomStore
from ..rules.v2_store import EvidenceProjector, RuleV2Store
from ..storage.database import open_database
from ..storage.layout import WorkspaceV2Layout
from ..storage.schema import SchemaError, initialize_all, initialize_database
from ..system.manifest import ManifestManager, ManifestState
from .content import V1ContentMigrator
from .memory import V1MemoryMigrator
from .rules import V1RulesMigrator
from .v2_validator import V2MigrationValidator, V2ValidationResult


class V2MigrationError(RuntimeError):
    """A Phase 2 shadow build failed and was rolled back to V1_ACTIVE."""


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    forbidden = {"body", "raw_content", "content", "text", "transcript", "full_transcript", "evidence"}
    return {str(key): item for key, item in value.items() if str(key).casefold() not in forbidden}


def _sha256_json(value: Any) -> str:
    payload = json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _assert_no_reparse_ancestry(value: str | Path) -> Path:
    """Reject workspace/ancestor symlink or Windows reparse components."""

    candidate = Path(os.path.abspath(os.fspath(Path(value).expanduser())))
    chain = list(reversed(candidate.parents)) + [candidate]
    for component in chain:
        try:
            info = os.lstat(component)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(f"cannot inspect workspace path component: {component}") from exc
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x0400):
            raise ValueError(f"workspace cannot contain symlink or reparse component: {component}")
    return candidate


@dataclass
class V2CoordinatorResult:
    status: str
    migration_id: str
    manifest_state: str
    checkpoints: dict[str, Any] = field(default_factory=dict)
    domains: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    source_hashes: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in {"V2_BUILDING", "DRY_RUN"} and not self.errors

    @property
    def ready(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "ready": False,
            "can_promote": False,
            "migration_id": self.migration_id,
            "manifest_state": self.manifest_state,
            "checkpoints": dict(self.checkpoints),
            "domains": dict(self.domains),
            "validation": dict(self.validation),
            "source_hashes": dict(self.source_hashes),
            "errors": list(self.errors),
        }

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


class V2MigrationCoordinator:
    """Run Content/Memory/Rules shadow migrations under one manifest batch."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        data_home: str | Path | None = None,
        source_workspace: str | Path | None = None,
        source_data_home: str | Path | None = None,
        workspace_source_pointer: str | Path | None = None,
        global_source_pointer: str | Path | None = None,
        data_home_root: str | Path | None = None,
        migration_id: str | None = None,
        content_migrator: V1ContentMigrator | None = None,
        memory_migrator: V1MemoryMigrator | None = None,
        rules_migrator: V1RulesMigrator | None = None,
        validator: V2MigrationValidator | None = None,
        fault_hook: Callable[[str], Any] | None = None,
        fail_at: str | None = None,
        keep_building_on_failure: bool = False,
        expected_generation: int | None = None,
    ) -> None:
        self.workspace = _assert_no_reparse_ancestry(workspace)
        self.layout = WorkspaceV2Layout(self.workspace)
        selected_data_home = data_home_root if data_home_root is not None else data_home
        self.data_home = Path(selected_data_home).expanduser().resolve() if selected_data_home is not None else None
        self.source_workspace = _assert_no_reparse_ancestry(source_workspace) if source_workspace is not None else self.workspace
        self.source_data_home = Path(source_data_home).expanduser().resolve() if source_data_home is not None else self.data_home
        self.workspace_source_pointer = str(_assert_no_reparse_ancestry(workspace_source_pointer)) if workspace_source_pointer is not None else str(self.workspace)
        self.global_source_pointer = str(Path(global_source_pointer).expanduser().absolute()) if global_source_pointer is not None else None
        self.migration_id = str(migration_id or "")
        self.fault_hook = fault_hook
        self.fail_at = fail_at
        # The production workspace-prepare entry point keeps a failed shadow
        # build in V2_BUILDING for crash recovery.  Historical callers retain
        # rollback-to-V1 behaviour by leaving this opt-in flag false.
        self.keep_building_on_failure = bool(keep_building_on_failure)
        self.expected_generation = expected_generation
        self.manifest = ManifestManager(self.layout)
        self._content = content_migrator
        self._memory = memory_migrator
        self._rules = rules_migrator
        self._validator = validator or V2MigrationValidator(
            self.layout,
            data_home=self.data_home,
            migration_id=self.migration_id,
            source_workspace=self.source_workspace,
            source_data_home=self.source_data_home,
        )
        self.last_result: V2CoordinatorResult | None = None

    @property
    def state(self) -> ManifestState:
        return self.manifest.current().state

    @property
    def checkpoints(self) -> dict[str, Any]:
        return dict(self.manifest.current().checkpoints)

    def _fault(self, step: str) -> None:
        if self.fail_at and self.fail_at == step:
            raise V2MigrationError(f"injected phase2 failure at {step}")
        if self.fault_hook is not None:
            self.fault_hook(step)

    def _source_hashes(self) -> dict[str, str]:
        inventory = self._validator.source_inventory()
        return {str(key): str(item["sha256"]) for key, item in inventory.items() if item.get("status") == "READY" and item.get("sha256")}

    def _manifest_source_hashes(self) -> tuple[bool, dict[str, str]]:
        current = self.manifest.current()
        if not isinstance(current.checkpoints, Mapping) or "phase2_sources" not in current.checkpoints:
            return False, {}
        checkpoint = current.checkpoints.get("phase2_sources", {})
        hashes = checkpoint.get("hashes", {}) if isinstance(checkpoint, Mapping) else {}
        return True, ({str(key): str(value) for key, value in hashes.items()} if isinstance(hashes, Mapping) else {})

    def _pointers(self) -> dict[str, str]:
        # Source pointers are immutable manifest evidence.  An old V1_ACTIVE
        # workspace may already have a workspace pointer but deliberately has
        # no global/data-home pointer.  Do not opportunistically add those
        # during cutover: that turns a harmless retry into an immutable pointer
        # mutation failure.  Global data-home pointers belong to a separately
        # initialized manifest, not Phase 2 shadow creation.
        current = self.manifest.current()
        if str(current.global_source_pointer or "") in {"", "NOT_CONFIGURED"} and str(current.data_home_root or "") in {"", "NOT_CONFIGURED"}:
            return {
                "workspace_source_pointer": self.workspace_source_pointer,
                "global_source_pointer": "NOT_CONFIGURED",
                "data_home_root": "NOT_CONFIGURED",
            }
        if self.data_home is None and self.global_source_pointer is None:
            return {
                "workspace_source_pointer": self.workspace_source_pointer,
                "global_source_pointer": "NOT_CONFIGURED",
                "data_home_root": "NOT_CONFIGURED",
            }
        global_pointer = self.global_source_pointer or str(self.data_home / "knowledge" / "knowledge.db")
        data_root = str(self.data_home) if self.data_home is not None else "NOT_CONFIGURED"
        return {
            "workspace_source_pointer": self.workspace_source_pointer,
            "global_source_pointer": global_pointer,
            "data_home_root": data_root,
        }

    def _ensure_building(self) -> None:
        current = self.manifest.current()
        if current.state is ManifestState.V1_ACTIVE:
            self.migration_id = self.migration_id or uuid.uuid4().hex
            if self.expected_generation is None:
                self.manifest.begin(migration_id=self.migration_id, **self._pointers())
            else:
                # ``ManifestManager.begin`` predates generation CAS; use its
                # equivalent transition API when the safe workspace wrapper
                # supplies an explicit expected generation.
                self.manifest.transition(
                    ManifestState.V2_BUILDING,
                    migration_id=self.migration_id,
                    expected_generation=self.expected_generation,
                    **self._pointers(),
                )
            # Subsequent append-only checkpoint writes CAS against the
            # BUILDING generation created above, not caller's pre-transition
            # generation.
            self.expected_generation = self.manifest.current().generation
        elif current.state is ManifestState.V2_BUILDING:
            self.migration_id = self.migration_id or current.migration_id
            if current.migration_id and self.migration_id != current.migration_id:
                raise V2MigrationError("existing V2_BUILDING batch has a different migration_id")
            self.expected_generation = current.generation
        else:
            raise V2MigrationError(f"cannot run Phase 2 from manifest state {current.state.value}")

    def _rules_sink(self, evidence_store: EvidenceStore) -> Callable[[Mapping[str, Any]], Any]:
        def write(reference: Mapping[str, Any]) -> Any:
            source_ref = str(reference.get("source_ref") or reference.get("evidence_ref") or "")
            if not source_ref:
                raise ValueError("rule evidence reference has no source_ref")
            # Projection is retried after a failed batch with a new
            # migration_id/event_id.  Those transport identities are not
            # evidence facts: keeping them in metadata would turn a replay of
            # the same evidence_id into a false conflict.  Persist only the
            # stable source/subject fields (after the common body scrub).
            metadata = _safe_metadata(reference)
            for volatile in ("migration_id", "event_id", "created_at", "updated_at", "consumed_at"):
                metadata.pop(volatile, None)
            evidence = evidence_store._put_evidence_for_migration(
                evidence_id=str(reference.get("evidence_id") or ""),
                source_ref=source_ref,
                revision=str(reference.get("revision") or reference.get("observed_at") or ""),
                digest=str(reference.get("content_digest") or reference.get("digest") or ""),
                authority="rule_migration",
                metadata=metadata,
                capability=_EVIDENCE_MIGRATION_CAPABILITY,
            )
            evidence_store._link_for_migration(
                evidence.evidence_id,
                str(reference.get("subject_type") or "rule"),
                str(reference.get("subject_id") or reference.get("definition_id") or reference.get("evidence_id") or "migration"),
                str(reference.get("relation") or "supports"),
                capability=_EVIDENCE_MIGRATION_CAPABILITY,
            )
            return evidence.evidence_id
        return write

    def _build_migrators(self) -> tuple[V1ContentMigrator, V1MemoryMigrator, V1RulesMigrator, MemoryAtomStore, EvidenceStore, RuleV2Store]:
        immutable_sources = self.source_workspace != self.workspace or self.source_data_home != self.data_home
        content = self._content or V1ContentMigrator(
            self.source_workspace,
            data_home=self.source_data_home,
            layout=self.layout,
            immutable_sources=immutable_sources,
        )
        memory_store = MemoryAtomStore(self.layout)
        evidence_store = EvidenceStore(self.layout)
        memory = self._memory or V1MemoryMigrator(
            self.source_workspace,
            target=self.layout,
            memory_store=memory_store,
            evidence_store=evidence_store,
            immutable_sources=immutable_sources,
        )
        rule_store = RuleV2Store(self.workspace)
        rules = self._rules or V1RulesMigrator(
            self.source_workspace,
            store=rule_store,
            migration_id=self.migration_id,
            evidence_sink=self._rules_sink(evidence_store),
            immutable_sources=immutable_sources,
        )
        return content, memory, rules, memory_store, evidence_store, rule_store

    @staticmethod
    def _legacy_phase2_marker(path: Path, domain: str) -> bool:
        """Recognize early shadow builds that overwrote shared schema_meta.

        Current stores keep Phase-2 markers in domain-owned metadata tables.
        Existing temporary fixtures may still carry the old marker in the
        shared Phase-1 table; allow their domain store to repair it, but never
        swallow an unknown marker.
        """

        expected = {
            "memory": "memoryguard-v2-phase2-memory",
            "evidence": "memoryguard-v2-phase2-evidence",
        }.get(domain)
        if not expected or not path.is_file():
            return False
        try:
            with open_database(path, readonly=True) as conn:
                row = conn.execute("SELECT marker FROM schema_meta WHERE domain=?", (domain,)).fetchone()
            return bool(row and str(row[0]) == expected)
        except (OSError, sqlite3.Error):
            return False

    def _initialize_v2_targets(self) -> None:
        """Initialize each target path without re-opening Phase-2 as Phase-1.

        Fresh builds use ``initialize_all``.  On rerun, existing files are
        validated individually; domain stores then perform their idempotent
        additive upgrades.  Only the two known early marker layouts are
        tolerated, and only for their owning store to repair.
        """

        self.layout.ensure_dirs()
        paths = tuple(self.layout.iter_db_paths())
        if not any(path.is_file() for _domain, path in paths):
            initialize_all(self.layout)
            return
        for domain, path in paths:
            try:
                initialize_database(path, domain, layout=self.layout)
            except SchemaError:
                if not self._legacy_phase2_marker(path, domain):
                    raise

    @staticmethod
    def _stable_checkpoint_value(key: str, value: Mapping[str, Any]) -> dict[str, Any]:
        """Keep immutable manifest checkpoints deterministic across reruns."""

        if key == "memory_migrated":
            counts = value.get("counts") if isinstance(value.get("counts"), Mapping) else {}
            digests = value.get("digests") if isinstance(value.get("digests"), Mapping) else {}
            return {
                "status": str(value.get("status") or ""),
                "ok": bool(value.get("ok")),
                "source_records": int(value.get("source_records") or counts.get("source_records") or 0),
                "atoms": int(value.get("atoms") or counts.get("atoms") or 0),
                "orphan": int(value.get("orphan") or counts.get("orphan") or 0),
                "scope_digest": str(value.get("scope_digest") or digests.get("scope") or ""),
                "source_digest": str(value.get("source_digest") or digests.get("source") or ""),
                "errors": list(value.get("errors") or []),
            }
        if key == "rules_migrated":
            counts = value.get("counts") if isinstance(value.get("counts"), Mapping) else {}
            return {
                "status": str(value.get("status") or ""),
                "ok": bool(value.get("ok")),
                "records": int(value.get("records") or counts.get("records") or 0),
                "binding_multiset_diff": int(value.get("binding_multiset_diff") or 0),
                "system_auto_expansion": int(value.get("system_auto_expansion") or 0),
                "evidence_status": str(value.get("evidence_status") or ""),
                "evidence_pending": int(value.get("evidence_pending") or 0),
                "source_digest": str(value.get("source_digest") or ""),
                "errors": list(value.get("errors") or []),
            }
        if key == "content_migrated":
            return {
                "status": str(value.get("status") or ""),
                "history_status": str(value.get("history_status") or ""),
                "knowledge_status": str(value.get("knowledge_status") or ""),
                "source_counts": dict(value.get("source_counts") or {}),
                "target_counts": dict(value.get("target_counts") or {}),
                "migration_map_count": int(value.get("migration_map_count") or 0),
                "errors": list(value.get("errors") or []),
            }
        if key == "outbox_drained":
            return {
                "memory": {item: int((value.get("memory") or {}).get(item, 0)) for item in ("projected", "failed", "pending")},
                "rules": {item: int((value.get("rules") or {}).get(item, 0)) for item in ("seen", "consumed", "pending")},
            }
        if key == "phase2_data_validated":
            domains = value.get("domains") if isinstance(value.get("domains"), Mapping) else {}
            return {
                "status": str(value.get("status") or ""),
                "ok": bool(value.get("ok")),
                "domain_status": {str(name): str((item or {}).get("status") or "") for name, item in domains.items() if isinstance(item, Mapping)},
                "errors": list(value.get("errors") or []),
                "metrics_digest": _sha256_json(value.get("metrics") or {}),
            }
        return dict(value)

    def _record_checkpoint(self, checkpoint: Mapping[str, Any]) -> None:
        """Record one step; changed retries append an authoritative attempt."""

        if not checkpoint:
            return
        key, value = next(iter(checkpoint.items()))
        stable = self._stable_checkpoint_value(str(key), value if isinstance(value, Mapping) else {"value": value})
        self.manifest.record_checkpoint_attempt(
            {str(key): stable},
            migration_id=self.migration_id,
            expected_generation=self.expected_generation,
        )

    def run(self, *, dry_run: bool = False, strict: bool = True) -> V2CoordinatorResult:
        if dry_run:
            current = self.manifest.current()
            effective_migration_id = self.migration_id or (
                current.migration_id
                if current.state is ManifestState.V2_BUILDING
                else ""
            )
            validation = self._validator.validate(
                migration_id=effective_migration_id
            )
            result = V2CoordinatorResult(
                "DRY_RUN",
                effective_migration_id,
                current.state.value,
                checkpoints=dict(current.checkpoints),
                validation=validation.to_dict(),
                errors=list(validation.errors),
            )
            self.last_result = result
            return result
        self._ensure_building()
        source_hashes = self._source_hashes()
        result = V2CoordinatorResult("V2_BUILDING", self.migration_id, ManifestState.V2_BUILDING.value, source_hashes=source_hashes)
        try:
            has_expected_sources, expected_source_hashes = self._manifest_source_hashes()
            if has_expected_sources and expected_source_hashes != source_hashes:
                raise V2MigrationError("phase2 source hash changed; immutable phase2_sources checkpoint blocks rerun")
            self._record_checkpoint({"phase2_started": {"migration_id": self.migration_id}})
            frozen = self.source_workspace != self.workspace or self.source_data_home != self.data_home
            self._record_checkpoint({"phase2_sources": {
                "hashes": source_hashes,
                "pointers": self._pointers(),
                "snapshot": {
                    "mode": "frozen" if frozen else "live",
                    "workspace": str(self.source_workspace),
                    "data_home": (str(self.source_data_home) if self.source_data_home is not None else "NOT_CONFIGURED"),
                },
            }})
            self._fault("phase2_sources")
            self._initialize_v2_targets()
            # Ensure the additive Content Plane tables exist even when all
            # configured V1 sources are absent.
            ContentStore(self.layout)
            self._record_checkpoint({"v2_initialized": {"domains": list(self.layout.DOMAINS)}})
            self._fault("v2_initialized")
            content, memory, rules, memory_store, evidence_store, rule_store = self._build_migrators()
            content_report = content.migrate()
            result.domains["content"] = content_report.to_dict()
            self._record_checkpoint({"content_migrated": content_report.to_dict()})
            self._fault("content_migrated")
            memory_report = memory.migrate(strict=strict)
            result.domains["memory"] = memory_report.to_dict()
            self._record_checkpoint({"memory_migrated": memory_report.to_dict()})
            self._fault("memory_migrated")
            rules_report = rules.migrate(evidence_sink=self._rules_sink(evidence_store))
            result.domains["rules"] = rules_report.to_dict()
            self._record_checkpoint({"rules_migrated": rules_report.to_dict()})
            self._fault("rules_migrated")
            memory_projection = memory_store.project_evidence(evidence_store)
            rule_projection = EvidenceProjector(rule_store, self._rules_sink(evidence_store)).project(migration_id=self.migration_id)
            drained = {"memory": memory_projection, "rules": rule_projection}
            result.domains["outbox"] = drained
            if int(memory_projection.get("failed", 0)) or int(memory_projection.get("pending", 0)) or int(rule_projection.get("pending", 0)):
                raise V2MigrationError(f"evidence outbox did not drain: {drained}")
            self._record_checkpoint({"outbox_drained": drained})
            self._fault("outbox_drained")
            validator = self._validator
            validator.expected_source_hashes = source_hashes
            validation = validator.validate(migration_id=self.migration_id)
            result.validation = validation.to_dict()
            if not validation.ok:
                raise V2MigrationError("phase2 validation blocked: " + "; ".join(validation.errors[:8]))
            self._record_checkpoint({"phase2_data_validated": validation.to_dict()})
            self._fault("phase2_data_validated")
            current = self.manifest.current()
            if current.state is not ManifestState.V2_BUILDING:
                raise V2MigrationError(f"Phase 2 changed manifest state unexpectedly: {current.state.value}")
            result.checkpoints = dict(current.checkpoints)
            result.manifest_state = current.state.value
            self.last_result = result
            return result
        except Exception as exc:
            result.status = "FAILED"
            result.errors.append(f"{type(exc).__name__}: {exc}")
            try:
                current = self.manifest.current()
                if self.keep_building_on_failure and current.state is ManifestState.V2_BUILDING:
                    # Persist failure evidence while retaining the BUILDING
                    # state.  The checkpoint is immutable and can be resumed
                    # by a later invocation with the same migration_id.
                    failure_checkpoint = {
                        "phase2_failed": {
                            "status": "FAILED",
                            "error": str(exc),
                            "migration_id": self.migration_id,
                        }
                    }
                    try:
                        self.manifest.record_checkpoint_attempt(
                            failure_checkpoint, migration_id=self.migration_id
                        )
                    except Exception as checkpoint_exc:
                        result.errors.append(
                            f"failure_checkpoint:{type(checkpoint_exc).__name__}: {checkpoint_exc}"
                        )
                elif current.state is not ManifestState.V1_ACTIVE:
                    self.manifest.fail(error=str(exc), migration_id=self.migration_id, errors={"phase2": result.errors, "checkpoints": current.checkpoints})
            except Exception as rollback_exc:
                result.errors.append(f"rollback_failed:{type(rollback_exc).__name__}: {rollback_exc}")
            result.manifest_state = self.manifest.current().state.value
            self.last_result = result
            if strict:
                raise V2MigrationError(str(exc)) from exc
            return result

    migrate = run
    execute = run
    dry_run = lambda self: self.run(dry_run=True)


__all__ = ["V2CoordinatorResult", "V2MigrationCoordinator", "V2MigrationError"]
