#!/usr/bin/env python3
"""Isolated Phase 8 migration, activation and rollback rehearsal.

The control workspace is always read-only.  Every write is confined to a new
temporary fixture populated through SQLite's online-backup API.  The fixture
is removed before the machine-readable report is returned.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import gc
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable
import uuid


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memoryguard.cutover_v2 import ReadinessGate, V2RuntimeFacade  # noqa: E402
from memoryguard.maintenance_v2.store import MaintenanceStore  # noqa: E402
from memoryguard.migration.v2_coordinator import V2MigrationCoordinator  # noqa: E402
from memoryguard.migration.v2_validator import V2MigrationValidator  # noqa: E402
from memoryguard.skills_v2.store import SkillStore  # noqa: E402
from memoryguard.storage.layout import WorkspaceV2Layout  # noqa: E402
from memoryguard.system.manifest import ManifestManager, ManifestState  # noqa: E402


class Phase8Error(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


@runtime_checkable
class ReadinessAssemblerProtocol(Protocol):
    def assemble(
        self,
        *,
        workspace: Path,
        migration: Mapping[str, Any],
        validation: Mapping[str, Any],
        generation: int,
        source_hashes: Mapping[str, str],
        target_hashes: Mapping[str, str],
    ) -> Mapping[str, Any]: ...


ReadinessAssembler = ReadinessAssemblerProtocol | Callable[..., Mapping[str, Any]]


@dataclass(frozen=True)
class SourceCopy:
    key: str
    source: Path
    target: Path
    before: Mapping[str, Any]


def _stable_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _synthetic_readiness_evidence(**kwargs: Any) -> dict[str, Any]:
    """Fixture-only evidence proving mechanics, never production readiness."""

    migration = kwargs["migration"]
    source_hashes = dict(kwargs["source_hashes"])
    target_hashes = dict(kwargs["target_hashes"])
    generation = int(kwargs["generation"])
    return {
        "metrics": {
            "loss": 0,
            "orphan": 0,
            "outbox": {"pending": 0, "failed": 0},
            "scope": 0,
            "binding": 0,
            "leak": 0,
            "mandatory_equivalence": True,
            "recall_v2": 1,
            "recall_v1": 1,
            "tokens_v2": 1,
            "tokens_v1": 2,
        },
        "source_digest": _stable_digest(source_hashes),
        "target_digest": _stable_digest(target_hashes),
        "manifest_digest": _stable_digest(
            {
                "generation": generation,
                "migration_id": migration["migration_id"],
                "source_digest": _stable_digest(source_hashes),
                "target_digest": _stable_digest(target_hashes),
            }
        ),
        "checkpoints": dict(migration["checkpoints"]),
        "validator_passed": True,
        "migration_id": migration["migration_id"],
        "generation": generation,
    }


def _fingerprint(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "size": 0, "sha256": ""}
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"exists": True, "size": path.stat().st_size, "sha256": digest.hexdigest()}


def _remove_disposable_tree(path: Path) -> str:
    """Remove an owned temp tree, tolerating brief Windows SQLite handles."""

    last_error = ""
    for _attempt in range(3):
        try:
            shutil.rmtree(path)
            return ""
        except FileNotFoundError:
            return ""
        except OSError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            gc.collect()
            time.sleep(0.05)
    return last_error


def _manifest_snapshot(workspace: Path) -> dict[str, Any]:
    manager = ManifestManager(workspace)
    path_fingerprint = _fingerprint(manager.db_path)
    try:
        record = manager.current()
        logical = {
            "state": record.state.value,
            "generation": record.generation,
            "migration_id": record.migration_id,
            "source_digest": record.source_digest,
            "target_digest": record.target_digest,
            "manifest_digest": record.manifest_digest,
            "workspace_source_pointer": record.workspace_source_pointer,
            "global_source_pointer": record.global_source_pointer,
            "data_home_root": record.data_home_root,
            "checkpoints": dict(record.checkpoints),
            "digests": dict(record.digests),
        }
    except Exception as exc:
        logical = {"error": f"{type(exc).__name__}: {exc}"}
    return {"file": path_fingerprint, "logical": logical}


def _inventory(
    workspace: Path,
    *,
    data_home: Path | None,
    selected_sources: Sequence[str] | None,
    require_ready: bool = True,
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
    inventory = V2MigrationValidator(workspace, data_home=data_home).source_inventory()
    ready = tuple(sorted(key for key, item in inventory.items() if item.get("status") == "READY"))
    selected = tuple(dict.fromkeys(str(key) for key in (selected_sources or ready)))
    if not selected and require_ready:
        raise Phase8Error("no_ready_sources", "control workspace has no READY V1 SQLite source")
    missing = [key for key in selected if key not in inventory]
    blocked = [key for key in selected if key in inventory and inventory[key].get("status") != "READY"]
    if missing:
        raise Phase8Error("unknown_source_selection", ",".join(missing))
    if blocked:
        raise Phase8Error(
            "source_not_ready",
            ";".join(f"{key}:{inventory[key].get('status')}" for key in blocked),
        )
    return inventory, selected


def _control_snapshot(
    workspace: Path,
    *,
    data_home: Path | None,
    selected_sources: Sequence[str] | None,
    require_ready: bool = False,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], tuple[str, ...]]:
    inventory, selected = _inventory(
        workspace,
        data_home=data_home,
        selected_sources=selected_sources,
        require_ready=require_ready,
    )
    sources = {
        key: {
            "path": str(inventory[key].get("path") or ""),
            **_fingerprint(Path(str(inventory[key]["path"]))),
        }
        for key in selected
    }
    return {
        "manifest": _manifest_snapshot(workspace),
        "sources": sources,
    }, inventory, selected


def _assert_safe_fixture(control: Path, fixture: Path) -> Path:
    control = control.resolve()
    fixture = fixture.resolve()
    try:
        fixture.relative_to(control)
        raise Phase8Error("unsafe_fixture_target", "fixture is the control workspace or its descendant")
    except ValueError:
        pass
    try:
        control.relative_to(fixture)
        raise Phase8Error("unsafe_fixture_target", "fixture cannot contain the control workspace")
    except ValueError:
        pass
    if fixture.exists():
        raise Phase8Error("unsafe_fixture_target", "fixture target must not already exist")
    return fixture


def _destination_for_source(key: str, fixture: Path, fixture_data_home: Path) -> Path:
    if key == "history":
        return fixture / ".memoryguard" / "history" / "history.sqlite"
    if key == "knowledge":
        return fixture_data_home / "knowledge" / "knowledge.db"
    if key == "rule_intelligence":
        return fixture / ".memoryguard" / "rule-intelligence" / "memory.db"
    if key.startswith("memory:"):
        group_id = key.split(":", 1)[1]
        if not group_id or any(part in {"", ".", ".."} for part in Path(group_id).parts):
            raise Phase8Error("unsafe_source_key", key)
        return fixture / ".memoryguard" / "shared-memory" / group_id / "memory.db"
    raise Phase8Error("unsupported_source", key)


def _create_synthetic_v1(root: Path) -> Path:
    """Create the smallest representative V1 source set in disposable storage."""

    group_db = root / ".memoryguard" / "shared-memory" / "phase8-synthetic" / "memory.db"
    group_db.parent.mkdir(parents=True, exist_ok=True)
    body = "phase8 synthetic source"
    with sqlite3.connect(group_db) as conn:
        conn.executescript(
            """
            CREATE TABLE records(memory_id TEXT PRIMARY KEY, body TEXT, kind TEXT,
                status TEXT, confidence REAL, locked INTEGER, injection_policy TEXT,
                priority INTEGER, supersedes TEXT, provenance TEXT, agent_instance_id TEXT,
                created_at TEXT, updated_at TEXT, canonical_hash TEXT, dedup_domain TEXT);
            CREATE TABLE rule_assignments(memory_id TEXT, target_type TEXT, target_id TEXT,
                project_ref TEXT, effect TEXT, priority_override INTEGER,
                created_at TEXT, updated_at TEXT);
            """
        )
        conn.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "phase8-memory", body, "fact", "active", 1.0, 0, "relevant", 0,
                "[]", "[]", "phase8-agent", "", "",
                hashlib.sha256(body.encode()).hexdigest(), "relevant",
            ),
        )
        conn.execute(
            "INSERT INTO rule_assignments VALUES (?,?,?,?,?,?,?,?)",
            ("phase8-memory", "agent", "phase8-agent", "", "include", 0, "", ""),
        )

    history_db = root / ".memoryguard" / "history" / "history.sqlite"
    history_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(history_db) as conn:
        conn.executescript(
            """
            CREATE TABLE conversation_sessions(session_id TEXT PRIMARY KEY, external_id TEXT,
                title TEXT, provider TEXT, agent_instance_id TEXT, project_ref TEXT,
                share_group_id TEXT, created_at TEXT, imported_at TEXT);
            CREATE TABLE conversation_turns(turn_id TEXT PRIMARY KEY, session_id TEXT,
                ordinal INTEGER, role TEXT, content TEXT, created_at TEXT,
                event_key TEXT, content_hash TEXT);
            CREATE TABLE session_summaries(session_id TEXT PRIMARY KEY, summary TEXT,
                summary_kind TEXT, updated_at TEXT);
            """
        )
        content = "phase8 synthetic history"
        conn.execute(
            "INSERT INTO conversation_sessions VALUES (?,?,?,?,?,?,?,?,?)",
            ("phase8-session", "synthetic", "Phase 8", "fixture", "phase8-agent", "", "phase8-synthetic", "", ""),
        )
        conn.execute(
            "INSERT INTO conversation_turns VALUES (?,?,?,?,?,?,?,?)",
            (
                "phase8-turn", "phase8-session", 0, "user", content, "",
                "phase8-event", hashlib.sha256(content.encode()).hexdigest(),
            ),
        )
        conn.execute(
            "INSERT INTO session_summaries VALUES (?,?,?,?)",
            ("phase8-session", "synthetic summary", "import", ""),
        )

    data_home = root / "_data_home"
    knowledge_db = data_home / "knowledge" / "knowledge.db"
    knowledge_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(knowledge_db) as conn:
        conn.executescript(
            """
            CREATE TABLE books(book_id TEXT PRIMARY KEY,title TEXT,root_path TEXT,status TEXT,
                created_at TEXT,updated_at TEXT);
            CREATE TABLE documents(document_id TEXT PRIMARY KEY,book_id TEXT,relative_path TEXT,
                media_type TEXT,content_hash TEXT,status TEXT,updated_at TEXT);
            CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY,document_id TEXT,book_id TEXT,
                ordinal INTEGER,text TEXT,text_hash TEXT,sensitivity TEXT,active INTEGER,
                created_at TEXT);
            CREATE TABLE memory_candidates(candidate_id TEXT PRIMARY KEY,book_id TEXT,
                chunk_id TEXT,content TEXT,source_text_hash TEXT,status TEXT,created_at TEXT);
            """
        )
        conn.execute(
            "INSERT INTO books VALUES (?,?,?,?,?,?)",
            ("phase8-book", "Synthetic", "/phase8", "ready", "", ""),
        )
        conn.execute(
            "INSERT INTO documents VALUES (?,?,?,?,?,?,?)",
            ("phase8-document", "phase8-book", "fixture.md", "text/plain", "fixture", "active", ""),
        )
        conn.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?)",
            ("phase8-chunk", "phase8-document", "phase8-book", 0, "fixture", "fixture", "normal", 1, ""),
        )
    return data_home


def _online_backup(source: Path, target: Path) -> None:
    if not source.is_file():
        raise Phase8Error("source_missing_during_backup", str(source))
    target.parent.mkdir(parents=True, exist_ok=True)
    source_uri = source.resolve().as_uri() + "?mode=ro"
    source_conn = sqlite3.connect(source_uri, uri=True)
    try:
        target_conn = sqlite3.connect(str(target))
        try:
            source_conn.backup(target_conn, pages=256, sleep=0.01)
            integrity = str(target_conn.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise Phase8Error("backup_integrity_failed", f"{target}:{integrity}")
        finally:
            target_conn.close()
    finally:
        source_conn.close()


def _copy_sources(
    inventory: Mapping[str, Mapping[str, Any]],
    selected: Sequence[str],
    fixture: Path,
    fixture_data_home: Path,
) -> tuple[SourceCopy, ...]:
    copies: list[SourceCopy] = []
    for key in selected:
        source = Path(str(inventory[key].get("path") or ""))
        target = _destination_for_source(key, fixture, fixture_data_home)
        before = _fingerprint(source)
        _online_backup(source, target)
        copies.append(SourceCopy(key, source, target, before))
    return tuple(copies)


def _hash_targets(layout: WorkspaceV2Layout) -> dict[str, str]:
    result: dict[str, str] = {}
    for domain, path in layout.iter_db_paths():
        if domain == "system":
            continue
        fingerprint = _fingerprint(path)
        if not fingerprint["exists"]:
            raise Phase8Error("target_missing", str(path))
        result[f"{domain}:{path.name}"] = str(fingerprint["sha256"])
    return result


def _resolve_readiness_assembler(provided: ReadinessAssembler | None) -> ReadinessAssembler:
    if provided is not None:
        return provided
    candidates = (
        ("memoryguard.cutover_v2.evidence_assembler", "ReadinessEvidenceAssembler"),
        ("memoryguard.cutover_v2.readiness_assembler", "assemble_readiness_evidence"),
        ("memoryguard.cutover_v2.readiness_assembler", "ReadinessEvidenceAssembler"),
        ("memoryguard.cutover_v2.readiness", "assemble_readiness_evidence"),
        ("memoryguard.cutover_v2.readiness", "ReadinessEvidenceAssembler"),
    )
    for module_name, attribute in candidates:
        try:
            module = importlib.import_module(module_name)
        except (ImportError, ModuleNotFoundError):
            continue
        value = getattr(module, attribute, None)
        if value is None:
            continue
        if isinstance(value, type):
            return value
        if callable(value) or isinstance(value, ReadinessAssemblerProtocol):
            return value
    raise Phase8Error(
        "readiness_assembler_unavailable",
        "install an assembler implementing ReadinessAssemblerProtocol",
    )


def _assemble(
    assembler: ReadinessAssembler,
    **kwargs: Any,
) -> dict[str, Any]:
    if isinstance(assembler, type):
        native_coverage = kwargs.get("native_coverage")
        registry_digest = (
            str(native_coverage.get("registry_digest") or "")
            if isinstance(native_coverage, Mapping)
            else ""
        )
        instance = assembler(
            kwargs["workspace"],
            data_home=kwargs.get("data_home"),
            native_coverage=native_coverage,
            expected_source_hashes=kwargs.get("source_hashes"),
            expected_native_registry_digest=registry_digest,
            manifest_manager=kwargs.get("manifest_manager"),
        )
        value = instance.assemble()
    else:
        method = getattr(assembler, "assemble", None)
        value = method(**kwargs) if callable(method) else assembler(**kwargs)  # type: ignore[operator]
    assembly_evidence = getattr(value, "evidence", None)
    if assembly_evidence is not None:
        evidence_dict = getattr(assembly_evidence, "to_dict", None)
        if not callable(evidence_dict):
            raise Phase8Error("readiness_evidence_invalid", "assembly evidence is not serializable")
        result = dict(evidence_dict())
        transition_payload = getattr(value, "transition_payload", None)
        if isinstance(transition_payload, Mapping):
            result["_transition_payload"] = json.loads(
                json.dumps(dict(transition_payload), ensure_ascii=False, default=str)
            )
        assembly_dict = getattr(value, "to_public_dict", None) or getattr(value, "to_dict", None)
        if callable(assembly_dict):
            result["_assembly"] = dict(assembly_dict())
        return result
    if not isinstance(value, Mapping):
        raise Phase8Error("readiness_evidence_invalid", "assembler must return a mapping")
    return dict(value)


def _readonly_production_preflight(
    workspace: Path,
    *,
    data_home: Path | None,
    inventory: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Assemble real evidence without accepting or invoking transition payloads."""

    manager = ManifestManager(workspace)
    native_coverage = _native_coverage(workspace, manager)
    source_hashes = {
        str(key): str(item.get("sha256") or "")
        for key, item in inventory.items()
        if item.get("status") == "READY" and item.get("sha256")
    }
    raw = _assemble(
        _resolve_readiness_assembler(None),
        workspace=workspace,
        data_home=data_home,
        migration={},
        validation={},
        generation=manager.current().generation + 1,
        source_hashes=source_hashes,
        target_hashes={},
        native_coverage=native_coverage,
        manifest_manager=manager,
    )
    assembly = raw.get("_assembly")
    if not isinstance(assembly, Mapping):
        raise Phase8Error("real_preflight_receipt_unavailable")
    return {
        "status": "BLOCKED",
        "mechanics_only": True,
        "production_ready": False,
        "activation_capability": False,
        "reason": "fixture_only_rehearsal_never_activates_real_workspace",
        "readiness_status": str(assembly.get("status") or "BLOCKED"),
        "readiness": dict(assembly),
        "native_coverage": {
            "schema": native_coverage.get("schema"),
            "registry_digest": native_coverage.get("registry_digest"),
            "counts": dict(native_coverage.get("counts") or {}),
        },
        "unchanged": False,
    }


def _native_facade(workspace: Path, manager: ManifestManager) -> tuple[V2RuntimeFacade, Any]:
    try:
        module = importlib.import_module("memoryguard.runtime_v2.native_ports")
        native_class = getattr(module, "NativeV2RuntimePort", None)
    except (ImportError, ModuleNotFoundError):
        native_class = None
    if callable(native_class):
        port = native_class(workspace)
        return V2RuntimeFacade(manifest=manager, v2=port, workspace=str(workspace)), port
    try:
        module = importlib.import_module("memoryguard.cutover_v2.facade")
        factory = getattr(module, "get_v2_runtime_facade", None)
        if callable(factory):
            facade = factory(workspace=str(workspace))
            if isinstance(facade, V2RuntimeFacade):
                return facade, getattr(getattr(facade, "ports", None), "v2", None)
    except Exception as exc:
        raise Phase8Error("native_ports_unavailable", f"{type(exc).__name__}: {exc}") from exc
    raise Phase8Error("native_ports_unavailable", "NativeV2RuntimePort factory is unavailable")


def _coverage_blockers(value: Mapping[str, Any]) -> int:
    counts = value.get("counts")
    if isinstance(counts, Mapping) and isinstance(counts.get("blocker"), int):
        return int(counts["blocker"])
    total = 0
    surfaces = value.get("surfaces")
    if isinstance(surfaces, Mapping):
        for item in surfaces.values():
            if isinstance(item, Mapping) and isinstance(item.get("blocker"), int):
                total += int(item["blocker"])
    return total


def _native_coverage(workspace: Path, manager: ManifestManager) -> dict[str, Any]:
    _facade, port = _native_facade(workspace, manager)
    coverage_fn = getattr(port, "coverage", None)
    if not callable(coverage_fn):
        raise Phase8Error("native_coverage_unavailable")
    raw = coverage_fn()
    if not isinstance(raw, Mapping):
        raise Phase8Error("native_coverage_invalid")
    return dict(raw)


def _native_smoke(workspace: Path, manager: ManifestManager) -> dict[str, Any]:
    facade, port = _native_facade(workspace, manager)
    coverage: dict[str, Any] = {}
    coverage_fn = getattr(port, "coverage", None)
    if callable(coverage_fn):
        raw = coverage_fn()
        if not isinstance(raw, Mapping):
            raise Phase8Error("native_coverage_invalid")
        coverage = dict(raw)
    result = facade.dispatch_mcp(
        "memoryguard_memory_status",
        {},
        context={
            "workspace_id": str(workspace),
            "agent_instance_id": "phase8-rehearsal",
            "share_group_id": "phase8-synthetic",
        },
    )
    if not isinstance(result, Mapping) or result.get("ok") is not True or result.get("path") != "v2":
        raise Phase8Error("native_v2_smoke_failed", json.dumps(result, ensure_ascii=False, default=str))
    if result.get("state") != "V2_ACTIVE":
        raise Phase8Error("native_v2_smoke_wrong_state", str(result.get("state")))
    return {
        "mechanics_only": True,
        "production_ready": False,
        "activation_capability": False,
        "result": dict(result),
        "coverage": coverage,
        "coverage_blockers": _coverage_blockers(coverage),
    }


def _run_fixture(
    fixture: Path,
    *,
    inventory: Mapping[str, Mapping[str, Any]],
    selected: Sequence[str],
    migration_id: str,
    assembler: ReadinessAssembler,
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    progress = progress if progress is not None else {}
    fixture_data_home = fixture / "_data_home"
    copies = _copy_sources(inventory, selected, fixture, fixture_data_home)
    progress["online_backup"] = len(copies) == len(selected)
    copied_before = {item.key: _fingerprint(item.target) for item in copies}
    data_home = fixture_data_home if "knowledge" in selected else None

    coordinator = V2MigrationCoordinator(
        fixture,
        data_home=data_home,
        migration_id=migration_id,
    )
    migration = coordinator.run(dry_run=False, strict=True)
    if not migration.ok or migration.manifest_state != "V2_BUILDING":
        raise Phase8Error("phase2_shadow_failed", json.dumps(migration.to_dict(), ensure_ascii=False, default=str))
    progress["phase2_shadow"] = True

    # Phase 2 owns ten core databases.  Readiness/ReferenceAudit additionally
    # requires the fixture-only Skills and Maintenance domains.
    SkillStore(fixture)
    MaintenanceStore(fixture)
    auxiliary_domains_initialized = (
        (fixture / ".memoryguard" / "skills" / "skills.db").is_file()
        and (fixture / ".memoryguard" / "system" / "maintenance.db").is_file()
    )
    if not auxiliary_domains_initialized:
        raise Phase8Error("auxiliary_domain_initialization_failed")
    progress["auxiliary_domains_initialized"] = True

    validation = V2MigrationValidator(
        fixture,
        data_home=data_home,
        migration_id=migration_id,
        expected_source_hashes=migration.source_hashes,
    ).validate(migration_id=migration_id)
    if not validation.ok or validation.status != "PASS":
        raise Phase8Error("readonly_validation_failed", ";".join(validation.errors[:12]))
    progress["readonly_validate"] = True

    manager = ManifestManager(fixture)
    building = manager.current()
    states = [building.state.value]
    target_hashes = _hash_targets(WorkspaceV2Layout(fixture))
    native_coverage = _native_coverage(fixture, manager)
    migration_payload = migration.to_dict()
    migration_payload["checkpoints"] = dict(building.checkpoints)
    evidence = _assemble(
        assembler,
        workspace=fixture,
        migration=migration_payload,
        validation=validation.to_dict(),
        generation=building.generation + 1,
        source_hashes=dict(migration.source_hashes),
        target_hashes=target_hashes,
        data_home=data_home,
        native_coverage=native_coverage,
        manifest_manager=manager,
    )
    assembly = evidence.pop("_assembly", {})
    transition_payload = evidence.pop("_transition_payload", {})
    gate = ReadinessGate(evidence=evidence, manifest=manager)
    readiness = gate.evaluate()
    if not readiness.ready:
        assembler_blockers = [
            str(item.get("code") or "")
            for item in assembly.get("blockers", [])
            if isinstance(item, Mapping)
        ] if isinstance(assembly, Mapping) else []
        raise Phase8Error(
            "readiness_blocked",
            json.dumps(
                {
                    "gate_failures": list(readiness.failures),
                    "assembler_blockers": [item for item in assembler_blockers if item],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    progress["readiness"] = True

    ready_evidence = readiness.evidence
    ready_checkpoint_map = readiness.to_dict()["evidence"]["checkpoints"]
    prior_checkpoints = building.digests.get("checkpoints", {})
    checkpoint_conflicts = sorted(
        key
        for key, value in prior_checkpoints.items()
        if key in ready_checkpoint_map and ready_checkpoint_map[key] != value
    ) if isinstance(prior_checkpoints, Mapping) else ["invalid_build_checkpoint_metadata"]
    if checkpoint_conflicts:
        raise Phase8Error("readiness_checkpoint_conflict", ",".join(checkpoint_conflicts))
    if transition_payload:
        ready = manager.transition(ManifestState.V2_READY, **dict(transition_payload))
    else:
        ready = manager.transition(
            ManifestState.V2_READY,
            migration_id=building.migration_id,
            source_digest=ready_evidence.source_digest,
            target_digest=ready_evidence.target_digest,
            manifest_digest=ready_evidence.manifest_digest,
            digests={
                "validator_passed": True,
                "checkpoints": dict(ready_checkpoint_map),
                "evidence_digest": readiness.digest,
                "evidence_generation": building.generation + 1,
            },
            expected_generation=building.generation,
        )
    states.append(ready.state.value)
    active = gate.activate(
        manager,
        readiness,
        expected_generation=ready.generation,
    )
    states.append(active.state.value)
    native = _native_smoke(fixture, manager)
    progress["native_v2_smoke"] = True
    rolled_back = gate.rollback(
        manager,
        reason="phase8_fixture_rehearsal",
        expected_generation=active.generation,
    )
    states.append(rolled_back.state.value)
    progress["rollback_v1"] = rolled_back.state is ManifestState.V1_ACTIVE

    copied_after = {item.key: _fingerprint(item.target) for item in copies}
    source_after = {item.key: _fingerprint(item.source) for item in copies}
    source_before = {item.key: dict(item.before) for item in copies}
    return {
        "states": states,
        "migration": migration.to_dict(),
        "validation": validation.to_dict(),
        "readiness": readiness.to_dict(),
        "readiness_assembly": assembly,
        "native": native,
        "source_keys": list(selected),
        "source_before": source_before,
        "source_after": source_after,
        "copied_before": copied_before,
        "copied_after": copied_after,
        "checks": {
            "online_backup": len(copies) == len(selected),
            "phase2_shadow": migration.ok and migration.manifest_state == "V2_BUILDING",
            "auxiliary_domains_initialized": auxiliary_domains_initialized,
            "readonly_validate": validation.ok and validation.status == "PASS",
            "readiness": readiness.ready,
            "native_v2_smoke": True,
            "rollback_v1": rolled_back.state is ManifestState.V1_ACTIVE,
            "source_copies_unchanged": copied_before == copied_after,
            "control_sources_unchanged_during_run": source_before == source_after,
        },
    }


def build_report(
    workspace: str | Path,
    *,
    data_home: str | Path | None = None,
    fixture_workspace: str | Path | None = None,
    source_fixture: str | Path | None = None,
    source_data_home: str | Path | None = None,
    allow_large_copy: bool = False,
    selected_sources: Sequence[str] | None = None,
    migration_id: str = "",
    readiness_assembler: ReadinessAssembler | None = None,
) -> dict[str, Any]:
    control = Path(workspace).expanduser().resolve()
    control_data_home = Path(data_home).expanduser().resolve() if data_home is not None else None
    explicit_source_data_home = (
        Path(source_data_home).expanduser().resolve() if source_data_home is not None else None
    )
    report: dict[str, Any] = {
        "contract": "memoryguard-v2-phase8-rehearsal",
        "phase": 8,
        "ok": False,
        # This command rehearses mechanics in a disposable fixture.  It is
        # never an activation authority and must not be represented as a
        # production-readiness result, even when every fixture check passes.
        "status": "MECHANICS_ONLY",
        "mechanics_only": True,
        "production_ready": False,
        "activation_capability": False,
        "control_workspace": str(control),
        "fixture_workspace": "",
        "source_fixture": "",
        "source_mode": "",
        "states": [],
        "outcomes": {
            "synthetic_rehearsal": {
                "status": "PENDING",
                "source_mode": "",
                "mechanics_only": True,
                "production_ready": False,
                "activation_capability": False,
            },
            "real_workspace_preflight": {
                "status": "BLOCKED",
                "mechanics_only": True,
                "production_ready": False,
                "activation_capability": False,
                "reason": "fixture_only_rehearsal_never_activates_real_workspace",
                "unchanged": False,
            },
        },
        "checks": {
            "fixture_target_safe": False,
            "online_backup": False,
            "phase2_shadow": False,
            "auxiliary_domains_initialized": False,
            "readonly_validate": False,
            "readiness": False,
            "native_v2_smoke": False,
            "rollback_v1": False,
            "source_copies_unchanged": False,
            "control_unchanged": False,
            "fixture_cleaned": False,
            "source_fixture_cleaned": False,
            "real_preflight_blocked": True,
        },
        "error_code": "",
        "error": "",
        "cleanup_errors": [],
    }
    fixture: Path | None = None
    fixture_created = False
    source_root: Path | None = None
    synthetic_source_created = False
    before: dict[str, Any] | None = None
    control_selected: tuple[str, ...] = ()
    try:
        if not control.is_dir():
            raise Phase8Error("control_workspace_missing", str(control))
        before, control_inventory, control_selected = _control_snapshot(
            control,
            data_home=control_data_home,
            selected_sources=None,
            require_ready=False,
        )
        report["control_before"] = before
        report["outcomes"]["real_workspace_preflight"] = _readonly_production_preflight(
            control,
            data_home=control_data_home,
            inventory=control_inventory,
        )
        requested_fixture = (
            Path(fixture_workspace).expanduser()
            if fixture_workspace is not None
            else Path(tempfile.gettempdir()) / f"memoryguard-phase8-{uuid.uuid4().hex}"
        )
        fixture = _assert_safe_fixture(control, requested_fixture)
        report["fixture_workspace"] = str(fixture)
        report["checks"]["fixture_target_safe"] = True

        assembler = readiness_assembler or _synthetic_readiness_evidence
        report["synthetic_evidence"] = readiness_assembler is None

        if source_fixture is not None:
            source_root = Path(source_fixture).expanduser().resolve()
            if source_root == control and not allow_large_copy:
                raise Phase8Error(
                    "real_source_copy_requires_opt_in",
                    "copying control sources requires --allow-large-copy",
                )
            source_mode = "real_workspace_opt_in" if source_root == control else "explicit_source_fixture"
            if not source_root.is_dir():
                raise Phase8Error("source_fixture_missing", str(source_root))
            source_home = (
                explicit_source_data_home
                if explicit_source_data_home is not None
                else (control_data_home if source_root == control else source_root / "_data_home")
            )
        elif allow_large_copy:
            source_root = control
            source_home = control_data_home
            source_mode = "real_workspace_opt_in"
        else:
            if selected_sources:
                raise Phase8Error(
                    "source_selection_requires_explicit_source",
                    "--source requires --source-fixture or --allow-large-copy",
                )
            source_root = Path(tempfile.gettempdir()) / f"memoryguard-phase8-source-{uuid.uuid4().hex}"
            source_root = _assert_safe_fixture(control, source_root)
            source_root.mkdir(parents=True, exist_ok=False)
            synthetic_source_created = True
            source_home = _create_synthetic_v1(source_root)
            source_mode = "synthetic"

        source_home_path = Path(source_home).resolve() if source_home is not None else None
        fixture = _assert_safe_fixture(source_root, fixture)
        inventory, selected = _inventory(
            source_root,
            data_home=source_home_path,
            selected_sources=selected_sources,
            require_ready=True,
        )
        report["source_fixture"] = str(source_root)
        report["source_mode"] = source_mode
        report["outcomes"]["synthetic_rehearsal"]["source_mode"] = source_mode
        report["source_keys"] = list(selected)
        fixture.mkdir(parents=True, exist_ok=False)
        fixture_created = True
        fixture_result = _run_fixture(
            fixture,
            inventory=inventory,
            selected=selected,
            migration_id=migration_id or f"phase8-{uuid.uuid4().hex}",
            assembler=assembler,
            progress=report["checks"],
        )
        report.update({key: value for key, value in fixture_result.items() if key != "checks"})
        report["checks"].update(fixture_result["checks"])
        report["outcomes"]["synthetic_rehearsal"]["status"] = "PASS"
    except Phase8Error as exc:
        report["error_code"] = exc.code
        report["error"] = exc.detail
        report["outcomes"]["synthetic_rehearsal"]["status"] = "BLOCKED"
        report["outcomes"]["synthetic_rehearsal"]["reason"] = exc.code
    except Exception as exc:  # fail closed; stdout remains one JSON document
        report["error_code"] = "phase8_unexpected_error"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["outcomes"]["synthetic_rehearsal"]["status"] = "BLOCKED"
        report["outcomes"]["synthetic_rehearsal"]["reason"] = "phase8_unexpected_error"
    finally:
        if fixture_created and fixture is not None:
            cleanup_error = _remove_disposable_tree(fixture)
            if cleanup_error:
                report["error_code"] = report["error_code"] or "fixture_cleanup_failed"
                report["error"] = report["error"] or cleanup_error
                report["cleanup_errors"].append({"target": "fixture", "error": cleanup_error})
        report["checks"]["fixture_cleaned"] = fixture is None or not fixture.exists()
        if synthetic_source_created and source_root is not None:
            cleanup_error = _remove_disposable_tree(source_root)
            if cleanup_error:
                report["error_code"] = report["error_code"] or "source_fixture_cleanup_failed"
                report["error"] = report["error"] or cleanup_error
                report["cleanup_errors"].append({"target": "source_fixture", "error": cleanup_error})
        report["checks"]["source_fixture_cleaned"] = (
            not synthetic_source_created or source_root is None or not source_root.exists()
        )
        if before is not None:
            try:
                after, _inventory_after, selected_after = _control_snapshot(
                    control,
                    data_home=control_data_home,
                    selected_sources=control_selected,
                    require_ready=False,
                )
                report["control_after"] = after
                unchanged = before == after and selected_after == control_selected
                report["checks"]["control_unchanged"] = unchanged
                report["outcomes"]["real_workspace_preflight"]["unchanged"] = unchanged
            except Exception as exc:
                report["control_after"] = {"error": f"{type(exc).__name__}: {exc}"}
                report["checks"]["control_unchanged"] = False

    required = tuple(report["checks"])
    report["ok"] = not report["error_code"] and all(bool(report["checks"][key]) for key in required)
    report["status"] = "MECHANICS_ONLY" if report["ok"] else "BLOCKED"
    report["production_ready"] = False
    report["activation_capability"] = False
    report["failures"] = [key for key, value in report["checks"].items() if not value]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT, help="read-only V1 control workspace")
    parser.add_argument("--data-home", type=Path, help="read-only control-workspace knowledge DataHome")
    parser.add_argument("--fixture-workspace", type=Path, help="new disposable target; must be outside control workspace")
    parser.add_argument(
        "--source-fixture",
        type=Path,
        help="explicit V1 fixture to online-backup; default creates a minimal synthetic source",
    )
    parser.add_argument("--source-data-home", type=Path, help="knowledge DataHome for --source-fixture")
    parser.add_argument(
        "--allow-large-copy",
        action="store_true",
        help="explicitly permit online backup from --workspace (never writes to it)",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help="V1 inventory key; requires --source-fixture or --allow-large-copy",
    )
    parser.add_argument("--migration-id", default="")
    parser.add_argument("--json", action="store_true", help="accepted for automation; output is always JSON")
    args = parser.parse_args(argv)
    report = build_report(
        args.workspace,
        data_home=args.data_home,
        fixture_workspace=args.fixture_workspace,
        source_fixture=args.source_fixture,
        source_data_home=args.source_data_home,
        allow_large_copy=args.allow_large_copy,
        selected_sources=args.source or None,
        migration_id=args.migration_id,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
