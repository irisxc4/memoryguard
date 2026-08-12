"""Fail-closed Phase 8 readiness evidence assembly.

This module only reads existing evidence.  It never initializes a Store,
changes the cutover manifest, or grants an activation capability.  The
assembled :class:`ReadinessEvidence` is accepted directly by the existing
``ReadinessGate.evaluate`` method.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from ..maintenance_v2.adapters import SQLiteReadOnlyAdapter
from ..maintenance_v2.reference_audit import ReferenceAudit
from ..maintenance_v2.registry import DEFAULT_REGISTRY, DomainRegistry
from ..maintenance_v2.store import MaintenanceStore
from ..migration.v2_validator import V2MigrationValidator
from ..system.manifest import ManifestManager
from .gui_contract import visible_registry_issues
from .readiness import ReadinessEvidence, ReadinessGate, ReadinessResult, stable_digest
from .surfaces import (
    CLI_COMMAND_NAMES,
    GUI_METHOD_NAMES,
    GUI_MUTATION_NAMES,
    MCP_MUTATION_NAMES,
    MCP_TOOL_NAMES,
    RULE_MUTATION_GUI_NAMES,
    SAFE_BRIDGE_METHOD_NAMES,
)


SCHEMA = "memoryguard-v2-readiness-evidence-1"
NATIVE_COVERAGE_SCHEMA = "v2-native-coverage-1"
REQUIRED_NATIVE_SURFACES = ("mcp", "gui", "cli", "hook")
SAFE_NATIVE_STATUSES = frozenset({"implemented", "neutral", "neutral-read", "retired"})
KNOWN_NATIVE_STATUSES = SAFE_NATIVE_STATUSES | {"blocker"}
EXPECTED_NATIVE_NAMES = MappingProxyType({
    "mcp": frozenset(MCP_TOOL_NAMES),
    "gui": frozenset(GUI_METHOD_NAMES),
    "cli": frozenset(CLI_COMMAND_NAMES),
    "hook": frozenset({"bootstrap_hook"}),
})
EXPECTED_NATIVE_COUNTS = MappingProxyType({
    surface: len(names) for surface, names in EXPECTED_NATIVE_NAMES.items()
})
_NATIVE_ENTRY_KEYS = frozenset({
    "name", "status", "handler", "mutation", "reason",
    "canonical_name", "domain", "execution",
})
REQUIRED_CHECKPOINTS = (
    "phase2_sources",
    "v2_initialized",
    "content_migrated",
    "memory_migrated",
    "rules_migrated",
    "outbox_drained",
    "phase2_data_validated",
)
_MISSING = object()
_UNSAFE = frozenset({"BLOCKED", "FAILED", "UNKNOWN", "UNAVAILABLE", "NOT_EVALUATED"})


class _TrustedNativeCoverageFixture:
    """Private in-process capability used only by this module's unit tests."""

    __slots__ = ("_value",)

    def __init__(self, value: Mapping[str, Any]) -> None:
        self._value = value

    def coverage(self) -> Mapping[str, Any]:
        return self._value


def _bind_native_coverage_for_test(value: Mapping[str, Any]) -> Any:
    """Bind synthetic coverage without making JSON a production authority."""

    return _TrustedNativeCoverageFixture(value)


def _trusted_native_provider(provider: Any) -> bool:
    if type(provider) is _TrustedNativeCoverageFixture:
        return True
    try:
        from ..runtime_v2.native_ports import NativeV2RuntimePort
    except Exception:
        return False
    return type(provider) is NativeV2RuntimePort


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    return value


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    for name in ("to_dict", "as_dict", "public_dict", "to_public_dict"):
        method = getattr(value, name, None)
        if callable(method):
            result = method()
            if isinstance(result, Mapping):
                return {str(key): item for key, item in result.items()}
    if value is not None and hasattr(value, "__dict__"):
        return {str(key): item for key, item in vars(value).items()}
    return {}


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _status(value: Any, default: str = "NOT_EVALUATED") -> str:
    raw = getattr(value, "value", value)
    marker = str(raw or default).strip().upper()
    return marker or default


def _numeric(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _lookup(mapping: Mapping[str, Any], names: Sequence[str]) -> Any:
    normalized = {str(key).casefold().replace("-", "_"): value for key, value in mapping.items()}
    for name in names:
        key = name.casefold().replace("-", "_")
        if key in normalized:
            return normalized[key]
    return _MISSING


def _leaf_items(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).casefold().replace("-", "_")
            path = f"{prefix}.{key_text}" if prefix else key_text
            if isinstance(child, Mapping):
                items.extend(_leaf_items(child, path))
            else:
                items.append((path, child))
    return items


def _zero_from_leaves(value: Mapping[str, Any], suffixes: Sequence[str]) -> int | str:
    wanted = tuple(item.casefold().replace("-", "_") for item in suffixes)
    matches = [item for key, item in _leaf_items(value) if any(key == suffix or key.endswith("." + suffix) or key.endswith("_" + suffix) for suffix in wanted)]
    if not matches:
        return "NOT_EVALUATED"
    numbers = [_numeric(item) for item in matches]
    if any(item is None for item in numbers):
        return "BLOCKED"
    total = sum(float(item) for item in numbers if item is not None)
    return int(total) if total.is_integer() else total


def source_set_digest(hashes: Mapping[str, str]) -> str:
    """Digest exact source key/hash set, independent of mapping order."""

    normalized = {str(key): str(value) for key, value in hashes.items()}
    return stable_digest(normalized)


def _audit_pages(value: Any) -> list[dict[str, Any]]:
    pages = _value(value, "pages", ()) or ()
    result: list[dict[str, Any]] = []
    for page in pages:
        domain = str(_value(page, "domain", ""))
        if domain == "system":
            # Manifest state/generation changes at cutover.  It has its own
            # digest below and must not make target data digest circular.
            continue
        rows = _value(page, "rows", ()) or ()
        result.append({
            "domain": domain,
            "table": str(_value(page, "table", "")),
            "fingerprint": str(_value(page, "fingerprint", "")),
            "rows": [str(_value(row, "row_hash", "")) for row in rows],
        })
    result.sort(key=lambda item: (item["domain"], item["table"], item["fingerprint"], item["rows"]))
    return result


def target_snapshot_digest(audit: Any, *, maintenance_fingerprint: str = "") -> str:
    """Digest audited target schema and public row hashes."""

    return stable_digest({
        "registry_digest": str(_value(audit, "registry_digest", "")),
        "schema_fingerprints": dict(_value(audit, "schema_fingerprints", {}) or {}),
        "pages": _audit_pages(audit),
        "maintenance_fingerprint": str(maintenance_fingerprint),
    })


def manifest_snapshot_digest(
    manifest: Any,
    *,
    source_digest: str | None = None,
    target_digest: str | None = None,
) -> str:
    """Digest immutable build identity without state/generation self-reference."""

    return stable_digest({
        "migration_id": str(_value(manifest, "migration_id", "")),
        "source_digest": str(_value(manifest, "source_digest", "") if source_digest is None else source_digest),
        "target_digest": str(_value(manifest, "target_digest", "") if target_digest is None else target_digest),
        "workspace_source_pointer": str(_value(manifest, "workspace_source_pointer", "")),
        "global_source_pointer": str(_value(manifest, "global_source_pointer", "")),
        "data_home_root": str(_value(manifest, "data_home_root", "")),
        "checkpoints": _plain(_value(manifest, "checkpoints", {}) or {}),
    })


@dataclass(frozen=True, slots=True)
class EvidenceBlocker:
    code: str
    component: str
    status: str = "BLOCKED"
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", MappingProxyType(dict(self.detail)))

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "component": self.component, "status": self.status, "detail": _plain(self.detail)}


@dataclass(frozen=True, slots=True)
class ReadinessEvidenceAssembly:
    status: str
    evidence: ReadinessEvidence
    gate_result: ReadinessResult
    blockers: tuple[EvidenceBlocker, ...]
    domains: Mapping[str, Any]
    diagnostics: Mapping[str, Any]
    candidate_evidence_digest: str
    expected_generation: int | None = None
    transition_payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "blockers", tuple(self.blockers))
        object.__setattr__(self, "domains", MappingProxyType(dict(self.domains)))
        object.__setattr__(self, "diagnostics", MappingProxyType(dict(self.diagnostics)))
        object.__setattr__(self, "transition_payload", MappingProxyType(dict(self.transition_payload)))

    @property
    def ready(self) -> bool:
        return self.status == "READY" and self.gate_result.ready and not self.blockers

    @property
    def ok(self) -> bool:
        return self.ready

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "status": self.status,
            "ok": self.ready,
            "ready": self.ready,
            "blockers": [item.to_dict() for item in self.blockers],
            "domains": _plain(self.domains),
            "diagnostics": _plain(self.diagnostics),
            "evidence": self.evidence.to_dict(),
            "evidence_digest": self.evidence.digest,
            "candidate_evidence_digest": self.candidate_evidence_digest,
            "expected_generation": self.expected_generation,
            "transition_payload": _plain(self.transition_payload),
            "readiness": self.gate_result.to_dict(),
        }

    as_dict = to_dict

    def to_public_dict(self) -> dict[str, Any]:
        """Machine receipt without paths, bodies, or checkpoint values."""

        evidence = self.evidence.to_dict()
        checkpoints = evidence.pop("checkpoints", {})
        evidence["checkpoint_keys"] = sorted(str(key) for key in checkpoints)
        evidence["checkpoints_digest"] = stable_digest(checkpoints)
        public_transition = {
            key: _plain(value)
            for key, value in self.transition_payload.items()
            if key not in {"digests"}
        }
        digest_map = self.transition_payload.get("digests")
        if isinstance(digest_map, Mapping):
            public_transition["digests"] = {
                "validator_passed": digest_map.get("validator_passed") is True,
                "evidence_digest": str(digest_map.get("evidence_digest") or ""),
                "evidence_generation": digest_map.get("evidence_generation"),
                "checkpoint_keys": sorted(str(key) for key in _mapping(digest_map.get("checkpoints"))),
                "checkpoints_digest": stable_digest(digest_map.get("checkpoints") or {}),
            }
        readiness = self.gate_result.to_dict()
        readiness.pop("evidence", None)
        readiness["checks"] = {
            str(key): {"ok": bool(_value(value, "ok", False))}
            for key, value in _mapping(readiness.get("checks")).items()
        }
        diagnostics = dict(self.diagnostics)
        diagnostics["phase4"] = _plain(diagnostics.get("phase4_metrics") or {})
        diagnostics.pop("phase4_metrics", None)
        return {
            "schema": SCHEMA,
            "status": self.status,
            "ok": self.ready,
            "ready": self.ready,
            "activation_capability": False,
            "blockers": [item.to_dict() for item in self.blockers],
            "domains": _plain(self.domains),
            "diagnostics": _plain(diagnostics),
            "evidence": evidence,
            "evidence_digest": self.evidence.digest,
            "candidate_evidence_digest": self.candidate_evidence_digest,
            "expected_generation": self.expected_generation,
            "transition_payload": public_transition,
            "readiness": readiness,
        }

    public_dict = to_public_dict


class ReadinessEvidenceAssembler:
    """Compose existing V2 proofs into one fail-closed gate input."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        data_home: str | Path | None = None,
        phase4_evidence: Any = None,
        native_coverage: Any = None,
        expected_source_hashes: Mapping[str, str] | None = None,
        expected_native_registry_digest: str = "",
        registry: DomainRegistry = DEFAULT_REGISTRY,
        validator: Any = None,
        reference_audit: Any = None,
        manifest_manager: Any = None,
        maintenance_provider: Callable[[], Mapping[str, Any]] | None = None,
        gate: ReadinessGate | None = None,
        page_size: int = 256,
        require_frozen_sources: bool = False,
        live_source_verifier: Callable[..., Mapping[str, Any]] | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser()
        self.data_home = None if data_home is None else Path(data_home).expanduser()
        self.phase4_evidence = phase4_evidence
        self.native_coverage = native_coverage
        self.expected_source_hashes = None if expected_source_hashes is None else {str(key): str(value) for key, value in expected_source_hashes.items()}
        self.expected_native_registry_digest = str(expected_native_registry_digest or "")
        self.registry = registry
        self.validator = validator
        self.reference_audit = reference_audit
        self.manifest_manager = manifest_manager
        self.maintenance_provider = maintenance_provider
        self.gate = gate or ReadinessGate()
        self.page_size = page_size
        self.require_frozen_sources = bool(require_frozen_sources)
        self.live_source_verifier = live_source_verifier

    @staticmethod
    def _block(blockers: list[EvidenceBlocker], code: str, component: str, *, status: str = "BLOCKED", **detail: Any) -> None:
        blockers.append(EvidenceBlocker(code, component, status, detail))

    @staticmethod
    def _resolve_provider(provider: Any, method: str = "") -> Any:
        if provider is None:
            return None
        if method:
            candidate = getattr(provider, method, None)
            if callable(candidate):
                return candidate()
        if callable(provider):
            return provider()
        return provider

    def _read_manifest(self, blockers: list[EvidenceBlocker]) -> Any:
        manager = self.manifest_manager or ManifestManager(self.workspace)
        try:
            manifest = manager.current() if callable(getattr(manager, "current", None)) else self._resolve_provider(manager)
        except Exception as exc:
            self._block(blockers, "manifest_unreadable", "manifest", error=type(exc).__name__)
            return {}
        state = _status(_value(manifest, "state", ""))
        if state not in {"V2_BUILDING", "V2_READY", "V2_ACTIVE"}:
            self._block(blockers, "manifest_state_not_ready_for_evidence", "manifest", state=state)
        generation = _value(manifest, "generation", None)
        if isinstance(generation, bool) or type(generation) is not int or generation < 0:
            self._block(blockers, "manifest_generation_invalid", "manifest")
        if not str(_value(manifest, "migration_id", "")):
            self._block(blockers, "migration_id_missing", "manifest", status="NOT_EVALUATED")
        checkpoints = _value(manifest, "checkpoints", {})
        if not isinstance(checkpoints, Mapping):
            self._block(blockers, "checkpoints_invalid", "manifest")
        else:
            missing = [key for key in REQUIRED_CHECKPOINTS if key not in checkpoints]
            if missing:
                self._block(blockers, "checkpoints_missing", "manifest", status="NOT_EVALUATED", missing=missing)
        return manifest

    def _snapshot_source_pointers(self, manifest: Any, blockers: list[EvidenceBlocker]) -> tuple[Path | None, Path | None, bool]:
        checkpoints = _value(manifest, "checkpoints", {})
        phase2 = checkpoints.get("phase2_sources") if isinstance(checkpoints, Mapping) else None
        snapshot = phase2.get("snapshot") if isinstance(phase2, Mapping) else None
        if not isinstance(snapshot, Mapping) or str(snapshot.get("mode") or "") != "frozen":
            return (None, None, False) if self.require_frozen_sources else (None, None, True)
        migration_id = str(_value(manifest, "migration_id", "") or "")
        allowed = (self.workspace / ".memoryguard" / "migration-backups" / migration_id / "source-snapshot").resolve(strict=False)
        raw_workspace = str(snapshot.get("workspace") or "")
        raw_data_home = str(snapshot.get("data_home") or "")
        if not raw_workspace:
            self._block(blockers, "source_snapshot_pointer_missing", "sources", status="NOT_EVALUATED")
            return None, None, False
        try:
            source_workspace = Path(raw_workspace).expanduser().resolve(strict=False)
            source_workspace.relative_to(allowed)
            source_data_home: Path | None = None
            if raw_data_home not in {"", "NOT_CONFIGURED"}:
                source_data_home = Path(raw_data_home).expanduser().resolve(strict=False)
                source_data_home.relative_to(allowed)
        except (OSError, ValueError):
            self._block(blockers, "source_snapshot_pointer_invalid", "sources")
            return None, None, False
        return source_workspace, source_data_home, True

    def _live_source_verification(self, manifest: Any, blockers: list[EvidenceBlocker]) -> dict[str, Any]:
        """Re-check live V1 at readiness time for production frozen builds."""

        if not self.require_frozen_sources:
            return {"status": "NOT_REQUIRED", "activation_safe": None}
        checkpoints = _value(manifest, "checkpoints", {})
        phase2 = checkpoints.get("phase2_sources") if isinstance(checkpoints, Mapping) else None
        snapshot = phase2.get("snapshot") if isinstance(phase2, Mapping) else None
        if not isinstance(snapshot, Mapping) or str(snapshot.get("mode") or "") != "frozen":
            self._block(blockers, "frozen_source_snapshot_required", "sources", status="NOT_EVALUATED")
            return {"status": "NOT_EVALUATED", "activation_safe": False}
        _source_workspace, _source_data_home, valid_snapshot = self._snapshot_source_pointers(manifest, blockers)
        if not valid_snapshot:
            return {"status": "BLOCKED", "activation_safe": False}
        migration_id = str(_value(manifest, "migration_id", "") or "")
        try:
            verifier = self.live_source_verifier
            if verifier is None:
                from ..migration.workspace_prepare import verify_v2_source_snapshot

                raw = verify_v2_source_snapshot(
                    self.workspace,
                    data_home=self.data_home,
                    migration_id=migration_id,
                )
            else:
                raw = verifier(
                    self.workspace,
                    data_home=self.data_home,
                    migration_id=migration_id,
                )
            result = _mapping(raw)
        except Exception as exc:
            self._block(blockers, "live_source_verification_unavailable", "sources", error=type(exc).__name__)
            return {"status": "BLOCKED", "activation_safe": False}
        status = _status(result.get("status"))
        activation_safe = result.get("activation_safe") is True
        snapshot_digest = str(result.get("snapshot_digest") or "")
        if status != "PASS" or not activation_safe:
            self._block(blockers, "live_source_drift_detected", "sources")
        if not snapshot_digest:
            self._block(blockers, "live_source_snapshot_digest_missing", "sources", status="NOT_EVALUATED")
        return {
            "status": status,
            "activation_safe": activation_safe,
            "checked": result.get("checked") if type(result.get("checked")) is int else None,
            "snapshot_digest": snapshot_digest,
        }

    def _validate_migration(self, manifest: Any, blockers: list[EvidenceBlocker]) -> tuple[Any, dict[str, Any]]:
        expected = self.expected_source_hashes
        validator = self.validator
        if validator is None:
            source_workspace, source_data_home, valid_snapshot = self._snapshot_source_pointers(manifest, blockers)
            if not valid_snapshot:
                return {}, {}
            validator = V2MigrationValidator(
                self.workspace,
                data_home=self.data_home,
                migration_id=str(_value(manifest, "migration_id", "")),
                expected_source_hashes=expected,
                source_workspace=source_workspace,
                source_data_home=source_data_home,
            )
        try:
            result = validator.validate() if callable(getattr(validator, "validate", None)) else self._resolve_provider(validator)
            data = _mapping(result)
        except Exception as exc:
            self._block(blockers, "validator_unavailable", "migration", error=type(exc).__name__)
            return {}, {}
        if _status(data.get("status")) != "PASS" or data.get("ok") is not True:
            self._block(blockers, "validator_not_passed", "migration", status=_status(data.get("status")))
        domains = data.get("domains")
        if not isinstance(domains, Mapping) or not domains:
            self._block(blockers, "validator_domains_missing", "migration", status="NOT_EVALUATED")
        else:
            bad = sorted(str(name) for name, item in domains.items() if _status(_value(item, "status", "")) != "PASS")
            if bad:
                self._block(blockers, "validator_domain_blocked", "migration", domains=bad)
        return result, data

    def _source_proof(self, validation: Mapping[str, Any], manifest: Any, blockers: list[EvidenceBlocker]) -> dict[str, Any]:
        actual = {str(key): str(value) for key, value in _mapping(validation.get("source_hashes")).items()}
        if self.expected_source_hashes is not None:
            expected: dict[str, str] | None = dict(self.expected_source_hashes)
            expected_from = "argument"
        else:
            checkpoints = _value(manifest, "checkpoints", {})
            phase2 = checkpoints.get("phase2_sources") if isinstance(checkpoints, Mapping) else None
            hashes = phase2.get("hashes") if isinstance(phase2, Mapping) else None
            expected = {str(key): str(value) for key, value in hashes.items()} if isinstance(hashes, Mapping) else None
            expected_from = "manifest"
        if expected is None:
            self._block(blockers, "expected_source_hashes_missing", "sources", status="NOT_EVALUATED")
            expected = {}
        elif actual != expected:
            self._block(
                blockers,
                "source_key_hash_set_mismatch",
                "sources",
                missing=sorted(set(expected) - set(actual)),
                unexpected=sorted(set(actual) - set(expected)),
                changed=sorted(key for key in set(actual) & set(expected) if actual[key] != expected[key]),
            )
        statuses = {str(key): _status(value) for key, value in _mapping(validation.get("source_status")).items()}
        blocked_sources = sorted(key for key, value in statuses.items() if value in _UNSAFE)
        if blocked_sources:
            self._block(blockers, "source_unreadable", "sources", sources=blocked_sources)
        expected_not_ready = sorted(key for key in expected if statuses.get(key) != "READY")
        if expected_not_ready:
            self._block(blockers, "expected_source_not_ready", "sources", sources=expected_not_ready)
        calculated = source_set_digest(expected)
        recorded = str(_value(manifest, "source_digest", ""))
        manifest_state = _status(_value(manifest, "state", ""))
        if not recorded and manifest_state != "V2_BUILDING":
            self._block(blockers, "source_digest_missing", "digests", status="NOT_EVALUATED")
        elif recorded != calculated:
            if recorded:
                self._block(blockers, "source_digest_mismatch", "digests")
        effective = recorded or calculated
        return {
            "status": "PASS" if expected is not None and actual == expected and not blocked_sources and not expected_not_ready and effective == calculated else "BLOCKED",
            "expected_from": expected_from,
            "expected_keys": sorted(expected),
            "actual_keys": sorted(actual),
            "expected_hashes": expected,
            "actual_hashes": actual,
            "source_status": statuses,
            "calculated_digest": calculated,
            "recorded_digest": recorded,
            "effective_digest": effective,
        }

    def _maintenance(self, blockers: list[EvidenceBlocker]) -> dict[str, Any]:
        try:
            if self.maintenance_provider is not None:
                result = dict(self.maintenance_provider())
            else:
                store = MaintenanceStore(self.workspace, readonly=True)
                snapshot = SQLiteReadOnlyAdapter(store.db_path, domain="maintenance").schema()
                result = {
                    "status": "PASS" if snapshot.ok else "BLOCKED",
                    "integrity": snapshot.integrity_check,
                    "foreign_key_errors": len(snapshot.foreign_key_errors),
                    "schema_fingerprint": snapshot.fingerprint,
                }
        except Exception as exc:
            self._block(blockers, "maintenance_unavailable", "maintenance", error=type(exc).__name__)
            return {"status": "BLOCKED", "integrity": "NOT_EVALUATED", "foreign_key_errors": "NOT_EVALUATED", "schema_fingerprint": ""}
        if _status(result.get("status")) != "PASS":
            self._block(blockers, "maintenance_blocked", "maintenance", status=_status(result.get("status")))
        if result.get("integrity") != "ok" or _numeric(result.get("foreign_key_errors")) != 0 or not str(result.get("schema_fingerprint") or ""):
            self._block(blockers, "maintenance_integrity_unproven", "maintenance")
        return result

    def _reference_proof(self, manifest: Any, maintenance: Mapping[str, Any], blockers: list[EvidenceBlocker]) -> tuple[Any, dict[str, Any]]:
        audit = self.reference_audit or ReferenceAudit(self.workspace, registry=self.registry, page_size=self.page_size)
        try:
            first = audit.audit() if callable(getattr(audit, "audit", None)) else self._resolve_provider(audit)
            if first is None:
                raise RuntimeError("first audit result missing")
            if callable(getattr(audit, "audit", None)):
                second = audit.audit(previous=first, prior_candidates=_value(first, "candidates", ()))
            else:
                second = first
        except Exception as exc:
            self._block(blockers, "reference_audit_unavailable", "reference_audit", error=type(exc).__name__)
            return {}, {"status": "BLOCKED"}
        expected_domains = tuple(self.registry.names)
        for epoch_name, result in (("epoch1", first), ("epoch2", second)):
            if _status(_value(result, "status", "")) != "PASS" or bool(_value(result, "blockers", ())):
                self._block(blockers, "reference_audit_blocked", "reference_audit", epoch=epoch_name)
            domains = tuple(str(item) for item in (_value(result, "domains", ()) or ()))
            if set(domains) != set(expected_domains) or len(domains) != len(expected_domains):
                self._block(blockers, "reference_domains_incomplete", "reference_audit", epoch=epoch_name, expected=list(expected_domains), actual=list(domains))
            fingerprints = dict(_value(result, "schema_fingerprints", {}) or {})
            if set(fingerprints) != set(expected_domains) or any(not str(item) for item in fingerprints.values()):
                self._block(blockers, "schema_fingerprints_incomplete", "reference_audit", epoch=epoch_name)
            if str(_value(result, "registry_digest", "")) != self.registry.digest:
                self._block(blockers, "reference_registry_digest_mismatch", "reference_audit", epoch=epoch_name)
            if _value(result, "manifest_generation", None) != _value(manifest, "generation", None):
                self._block(blockers, "reference_manifest_generation_mismatch", "reference_audit", epoch=epoch_name)
        maintenance_fingerprint = str(maintenance.get("schema_fingerprint") or "")
        first_digest = target_snapshot_digest(first, maintenance_fingerprint=maintenance_fingerprint)
        calculated = target_snapshot_digest(second, maintenance_fingerprint=maintenance_fingerprint)
        if first_digest != calculated:
            self._block(blockers, "reference_epoch_drift", "reference_audit")
        recorded = str(_value(manifest, "target_digest", ""))
        manifest_state = _status(_value(manifest, "state", ""))
        if not recorded and manifest_state != "V2_BUILDING":
            self._block(blockers, "target_digest_missing", "digests", status="NOT_EVALUATED")
        elif recorded != calculated:
            if recorded:
                self._block(blockers, "target_digest_mismatch", "digests")
        public = _mapping(second)
        public_method = getattr(second, "to_public_dict", None)
        if callable(public_method):
            public = dict(public_method())
        public.update({"epoch1_digest": first_digest, "epoch2_digest": calculated, "recorded_target_digest": recorded, "effective_target_digest": recorded or calculated})
        return second, public

    def _phase4(self, blockers: list[EvidenceBlocker]) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            raw = self._resolve_provider(self.phase4_evidence)
        except Exception as exc:
            self._block(blockers, "phase4_unavailable", "phase4", error=type(exc).__name__)
            raw = None
        data = _mapping(raw)
        if not data:
            self._block(blockers, "phase4_missing", "phase4", status="NOT_EVALUATED")
            data = {}
        elif data.get("ok") is not True:
            self._block(blockers, "phase4_not_passed", "phase4", status=_status(data.get("status")))
        mandatory = _lookup(data, ("mandatory_equivalence", "mandatory_equal", "mandatory_equivalent"))
        recall_v2 = _lookup(data, ("recall_v2", "recall_at_k", "v2_recall"))
        recall_v1 = _lookup(data, ("recall_v1", "v1_recall_at_k", "baseline_recall"))
        tokens = data.get("context_tokens")
        tokens_v2 = _lookup(tokens, ("v2", "current")) if isinstance(tokens, Mapping) else _lookup(data, ("tokens_v2", "v2_tokens", "context_tokens_v2"))
        tokens_v1 = _lookup(tokens, ("v1", "baseline")) if isinstance(tokens, Mapping) else _lookup(data, ("tokens_v1", "v1_tokens", "context_tokens_v1"))
        scope = _lookup(data, ("scope_leak_count", "scope_leak", "scope"))
        leak = _lookup(data, ("leak", "negative_evidence_leak", "runtime_leak", "v1_collision_runtime_leak"))
        deterministic = data.get("deterministic", _MISSING)
        required = {
            "mandatory_equivalence": mandatory,
            "recall_v2": recall_v2,
            "recall_v1": recall_v1,
            "tokens_v2": tokens_v2,
            "tokens_v1": tokens_v1,
            "scope": scope,
            "leak": leak,
            "deterministic": deterministic,
        }
        missing = sorted(key for key, value in required.items() if value is _MISSING)
        if missing:
            self._block(blockers, "phase4_metrics_missing", "phase4", status="NOT_EVALUATED", missing=missing)
        if mandatory is not _MISSING and mandatory is not True:
            self._block(blockers, "mandatory_equivalence_failed", "phase4")
        if deterministic is not _MISSING and deterministic is not True:
            self._block(blockers, "phase4_nondeterministic", "phase4")
        for name, value in (("scope", scope), ("leak", leak)):
            if value is not _MISSING and _numeric(value) != 0:
                self._block(blockers, f"{name}_nonzero", "phase4", value=value)
        r2, r1 = _numeric(recall_v2), _numeric(recall_v1)
        if recall_v2 is not _MISSING and recall_v1 is not _MISSING and (r2 is None or r1 is None or r1 < 0 or r2 < r1):
            self._block(blockers, "recall_below_v1", "phase4")
        t2, t1 = _numeric(tokens_v2), _numeric(tokens_v1)
        if tokens_v2 is not _MISSING and tokens_v1 is not _MISSING and (t2 is None or t1 is None or t2 < 0 or t1 < 0 or t2 >= t1):
            self._block(blockers, "tokens_not_lower", "phase4")
        metrics = {key: ("NOT_EVALUATED" if value is _MISSING else value) for key, value in required.items()}
        return data, metrics

    def _native(self, blockers: list[EvidenceBlocker]) -> dict[str, Any]:
        provider = self.native_coverage
        trusted = _trusted_native_provider(provider)
        if provider is not None and not trusted:
            self._block(
                blockers,
                "native_coverage_untrusted",
                "native_coverage",
                status="NOT_EVALUATED",
            )
        try:
            raw = self._resolve_provider(provider, "coverage")
        except Exception as exc:
            self._block(blockers, "native_coverage_unavailable", "native_coverage", error=type(exc).__name__)
            raw = None
        data = _mapping(raw)
        if not data:
            self._block(blockers, "native_coverage_missing", "native_coverage", status="NOT_EVALUATED")
            return {"status": "NOT_EVALUATED", "schema": "", "registry_digest": "", "surfaces": {}, "counts": {}}
        if str(data.get("schema") or "") != NATIVE_COVERAGE_SCHEMA:
            self._block(blockers, "native_coverage_schema_mismatch", "native_coverage")
        digest = str(data.get("registry_digest") or "")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest.casefold()):
            self._block(blockers, "native_registry_digest_invalid", "native_coverage")
        coverage_digest = str(data.get("coverage_digest") or "")
        if len(coverage_digest) != 64 or any(ch not in "0123456789abcdef" for ch in coverage_digest.casefold()):
            self._block(blockers, "native_coverage_digest_invalid", "native_coverage")
        surfaces = data.get("surfaces")
        if not isinstance(surfaces, Mapping):
            self._block(blockers, "native_surfaces_missing", "native_coverage", status="NOT_EVALUATED")
            surfaces = {}
        else:
            supplied_surfaces = {str(name) for name in surfaces}
            expected_surfaces = set(REQUIRED_NATIVE_SURFACES)
            if supplied_surfaces != expected_surfaces:
                self._block(
                    blockers,
                    "native_surface_set_mismatch",
                    "native_coverage",
                    missing=sorted(expected_surfaces - supplied_surfaces),
                    unexpected=sorted(supplied_surfaces - expected_surfaces),
                )
        totals = {status: 0 for status in ("implemented", "neutral", "retired", "blocker")}
        total_entries = 0
        canonical_registry: dict[str, list[dict[str, Any]]] = {}
        for surface in REQUIRED_NATIVE_SURFACES:
            item = surfaces.get(surface) if isinstance(surfaces, Mapping) else None
            if not isinstance(item, Mapping):
                self._block(blockers, "native_surface_missing", "native_coverage", status="NOT_EVALUATED", surface=surface)
                continue
            entries = item.get("entries")
            if isinstance(entries, (list, tuple)):
                entry_values = list(entries)
            else:
                self._block(blockers, "native_entries_missing", "native_coverage", status="NOT_EVALUATED", surface=surface)
                continue
            counts = {status: 0 for status in ("implemented", "neutral", "retired", "blocker")}
            canonical_entries: list[dict[str, Any]] = []
            names: list[str] = []
            for entry in entry_values:
                if not isinstance(entry, Mapping) or {str(key) for key in entry} != _NATIVE_ENTRY_KEYS:
                    self._block(blockers, "native_entry_shape_invalid", "native_coverage", surface=surface)
                    continue
                name = str(entry.get("name") or "")
                marker = str(entry.get("status") or "").casefold()
                handler = entry.get("handler")
                mutation = entry.get("mutation")
                reason = entry.get("reason")
                canonical_name = entry.get("canonical_name")
                domain = entry.get("domain")
                execution = entry.get("execution")
                names.append(name)
                if (
                    not name
                    or not isinstance(handler, str) or not handler
                    or type(mutation) is not bool
                    or not isinstance(reason, str)
                    or not isinstance(canonical_name, str) or not canonical_name
                    or not isinstance(domain, str)
                    or execution not in {"sync", "task"}
                ):
                    self._block(blockers, "native_entry_shape_invalid", "native_coverage", surface=surface, name=name)
                    continue
                if marker not in KNOWN_NATIVE_STATUSES:
                    self._block(blockers, "native_entry_status_invalid", "native_coverage", surface=surface, name=name)
                    continue
                canonical = "neutral" if marker in {"neutral", "neutral-read"} else marker
                counts[canonical] += 1
                totals[canonical] += 1
                if marker == "retired" and not reason:
                    self._block(blockers, "native_retired_reason_missing", "native_coverage", surface=surface, name=name)
                if marker not in SAFE_NATIVE_STATUSES:
                    self._block(blockers, "native_operation_blocked", "native_coverage", surface=surface, name=name)
                if surface == "gui" and marker != "implemented":
                    self._block(
                        blockers,
                        "native_gui_operation_not_implemented",
                        "native_coverage",
                        surface=surface,
                        name=name,
                        status=marker,
                    )
                expected_mutation: bool | None = None
                if surface == "mcp":
                    expected_mutation = name in MCP_MUTATION_NAMES
                elif surface == "gui":
                    expected_mutation = name in GUI_MUTATION_NAMES
                elif surface == "hook":
                    expected_mutation = False
                if expected_mutation is not None and mutation is not expected_mutation:
                    self._block(
                        blockers,
                        "native_mutation_classification_mismatch",
                        "native_coverage",
                        surface=surface,
                        name=name,
                    )
                canonical_entries.append({
                    "name": name,
                    "status": marker,
                    "handler": handler,
                    "mutation": mutation,
                    "reason": reason,
                    "canonical_name": canonical_name,
                    "domain": domain,
                    "execution": execution,
                })
            total_entries += len(entry_values)
            expected_names = EXPECTED_NATIVE_NAMES[surface]
            supplied_names = set(names)
            duplicate_names = sorted(name for name in supplied_names if names.count(name) > 1)
            if duplicate_names:
                self._block(blockers, "native_operation_names_duplicate", "native_coverage", surface=surface, names=duplicate_names)
            if supplied_names != expected_names or len(names) != EXPECTED_NATIVE_COUNTS[surface]:
                self._block(
                    blockers,
                    "native_operation_set_mismatch",
                    "native_coverage",
                    surface=surface,
                    expected_count=EXPECTED_NATIVE_COUNTS[surface],
                    actual_count=len(names),
                    missing=sorted(expected_names - supplied_names),
                    unexpected=sorted(supplied_names - expected_names),
                )
            canonical_registry[surface] = sorted(canonical_entries, key=lambda entry: entry["name"])
            declared_total = item.get("total")
            declared_neutral = item.get("neutral") if "neutral" in item else item.get("neutral-read")
            declared = {"implemented": item.get("implemented"), "neutral": declared_neutral, "retired": item.get("retired"), "blocker": item.get("blocker")}
            if type(declared_total) is not int or declared_total != len(entry_values) or any(type(declared.get(status)) is not int or declared.get(status) != counts[status] for status in counts):
                self._block(blockers, "native_surface_counts_mismatch", "native_coverage", surface=surface)
            if not entry_values:
                self._block(blockers, "native_surface_empty", "native_coverage", surface=surface)
        visible_issues: tuple[dict[str, str], ...] = ()
        if "gui" in canonical_registry:
            try:
                gui_by_name = {str(entry["name"]): entry for entry in canonical_registry["gui"]}
                visible_issues = visible_registry_issues(gui_by_name)
                for issue in visible_issues:
                    self._block(
                        blockers,
                        str(issue.get("code") or "visible_gui_method_invalid"),
                        "native_coverage",
                        surface="gui",
                        name=str(issue.get("name") or ""),
                        status=str(issue.get("status") or ""),
                    )
            except Exception:
                self._block(
                    blockers,
                    "visible_gui_scan_unavailable",
                    "native_coverage",
                    surface="gui",
                    status="NOT_EVALUATED",
                )

        declared_counts = data.get("counts")
        if not isinstance(declared_counts, Mapping):
            self._block(blockers, "native_counts_missing", "native_coverage", status="NOT_EVALUATED")
        else:
            expected_counts = {"total": total_entries, **totals}
            normalized_declared = dict(declared_counts)
            if "neutral" not in normalized_declared and "neutral-read" in normalized_declared:
                normalized_declared["neutral"] = normalized_declared["neutral-read"]
            if any(type(normalized_declared.get(key)) is not int or normalized_declared.get(key) != value for key, value in expected_counts.items()):
                self._block(blockers, "native_counts_mismatch", "native_coverage")
        if set(canonical_registry) == set(REQUIRED_NATIVE_SURFACES):
            canonical_digest = stable_digest(canonical_registry)
            if digest != canonical_digest:
                self._block(blockers, "native_registry_digest_mismatch", "native_coverage")
            if coverage_digest != canonical_digest:
                self._block(blockers, "native_coverage_digest_mismatch", "native_coverage")
        else:
            canonical_digest = ""
        # ``complete`` remains the historical blocker-only compatibility bit;
        # formal readiness additionally requires the explicit production bit,
        # which excludes diagnostic neutral-read entries.
        complete = data.get("complete")
        expected_complete = totals["blocker"] == 0
        if type(complete) is not bool or complete is not expected_complete:
            self._block(blockers, "native_complete_mismatch", "native_coverage")
        production_complete = data.get("production_complete", _MISSING)
        gui_item = surfaces.get("gui") if isinstance(surfaces, Mapping) else None
        gui_entries = gui_item.get("entries") if isinstance(gui_item, Mapping) else None
        gui_all_implemented = bool(gui_entries) and all(
            isinstance(entry, Mapping)
            and str(entry.get("status") or "").casefold() == "implemented"
            for entry in gui_entries
        )
        expected_production_complete = expected_complete and totals["neutral"] == 0 and gui_all_implemented
        if production_complete is _MISSING:
            self._block(blockers, "native_production_complete_missing", "native_coverage", status="NOT_EVALUATED")
        elif type(production_complete) is not bool or production_complete is not expected_production_complete:
            self._block(blockers, "native_production_complete_mismatch", "native_coverage")
        if expected_production_complete is not True:
            self._block(blockers, "native_production_incomplete", "native_coverage")
        return {
            "status": "PASS" if not any(item.component == "native_coverage" for item in blockers) else "BLOCKED",
            "schema": str(data.get("schema") or ""),
            "registry_digest": digest,
            "canonical_registry_digest": canonical_digest,
            "surfaces": _plain(surfaces),
            "counts": _plain(data.get("counts") or {}),
            "visible_gui_issues": [dict(item) for item in visible_issues],
            "complete": complete,
            "production_complete": None if production_complete is _MISSING else production_complete,
        }

    def assemble(self) -> ReadinessEvidenceAssembly:
        blockers: list[EvidenceBlocker] = []
        manifest = self._read_manifest(blockers)
        live_source = self._live_source_verification(manifest, blockers)
        validation_result, validation = self._validate_migration(manifest, blockers)
        sources = self._source_proof(validation, manifest, blockers)
        maintenance = self._maintenance(blockers)
        audit_result, audit = self._reference_proof(manifest, maintenance, blockers)
        phase4, phase4_metrics = self._phase4(blockers)
        native = self._native(blockers)

        validation_metrics = _mapping(validation.get("metrics"))
        loss = _zero_from_leaves(validation_metrics, ("loss", "loss_count"))
        orphan = _zero_from_leaves(validation_metrics, ("orphan", "orphan_count", "evidence_orphan"))
        binding = _zero_from_leaves(validation_metrics, ("binding", "binding_diff", "binding_multiset_diff", "binding_identity_multiset_diff"))
        scope_validation = _zero_from_leaves(validation_metrics, ("auto_scope_expansion", "scope_anomaly"))
        if loss == "NOT_EVALUATED":
            self._block(blockers, "loss_not_evaluated", "metrics", status="NOT_EVALUATED")
        elif loss == "BLOCKED" or _numeric(loss) != 0:
            self._block(blockers, "loss_nonzero", "metrics", value=loss)
        if orphan == "NOT_EVALUATED":
            self._block(blockers, "orphan_not_evaluated", "metrics", status="NOT_EVALUATED")
        elif orphan == "BLOCKED" or _numeric(orphan) != 0:
            self._block(blockers, "orphan_nonzero", "metrics", value=orphan)
        if binding == "NOT_EVALUATED":
            self._block(blockers, "binding_not_evaluated", "metrics", status="NOT_EVALUATED")
        elif binding == "BLOCKED" or _numeric(binding) != 0:
            self._block(blockers, "binding_nonzero", "metrics", value=binding)
        phase4_scope = phase4_metrics["scope"]
        scope = 0 if _numeric(scope_validation) == 0 and _numeric(phase4_scope) == 0 else ("NOT_EVALUATED" if "NOT_EVALUATED" in {scope_validation, phase4_scope} else "BLOCKED")
        outbox = 0 if _status(audit.get("status")) == "PASS" else "BLOCKED"

        component_status = {
            "migration": "PASS" if _status(validation.get("status")) == "PASS" and validation.get("ok") is True else "BLOCKED",
            "reference_audit": "PASS" if _status(audit.get("status")) == "PASS" else "BLOCKED",
            "maintenance": _status(maintenance.get("status")),
            "phase4": "PASS" if phase4.get("ok") is True else ("NOT_EVALUATED" if not phase4 else "BLOCKED"),
            "native_coverage": _status(native.get("status")),
            "live_source": _status(live_source.get("status")),
        }
        metrics = {
            "loss": loss,
            "orphan": orphan,
            "outbox": outbox,
            "scope": scope,
            "binding": binding,
            "leak": phase4_metrics["leak"],
            "mandatory_equivalence": phase4_metrics["mandatory_equivalence"],
            "recall_v2": phase4_metrics["recall_v2"],
            "recall_v1": phase4_metrics["recall_v1"],
            "tokens_v2": phase4_metrics["tokens_v2"],
            "tokens_v1": phase4_metrics["tokens_v1"],
            "integrity_errors": 0 if component_status["reference_audit"] == "PASS" and component_status["maintenance"] == "PASS" else "BLOCKED",
            "foreign_key_errors": 0 if component_status["reference_audit"] == "PASS" and component_status["maintenance"] == "PASS" else "BLOCKED",
            "unknown_authoritative": 0 if component_status["reference_audit"] == "PASS" and component_status["maintenance"] == "PASS" else "BLOCKED",
            "native_coverage": 0 if component_status["native_coverage"] == "PASS" else component_status["native_coverage"],
            "proof": component_status,
        }

        manifest_state = _status(_value(manifest, "state", ""))
        calculated_source_digest = str(sources.get("calculated_digest") or "")
        calculated_target_digest = str(audit.get("epoch2_digest") or "")
        recorded_manifest_digest = str(_value(manifest, "manifest_digest", ""))
        calculated_manifest_digest = manifest_snapshot_digest(
            manifest,
            source_digest=calculated_source_digest,
            target_digest=calculated_target_digest,
        )
        if not recorded_manifest_digest and manifest_state != "V2_BUILDING":
            self._block(blockers, "manifest_digest_missing", "digests", status="NOT_EVALUATED")
        elif recorded_manifest_digest and recorded_manifest_digest != calculated_manifest_digest:
            self._block(blockers, "manifest_digest_mismatch", "digests")

        checkpoints = _value(manifest, "checkpoints", {})
        current_generation = _value(manifest, "generation", None)
        manifest_digests = _value(manifest, "digests", {})
        if not isinstance(manifest_digests, Mapping):
            self._block(blockers, "manifest_digest_metadata_invalid", "digests")
            manifest_digests = {}
        if manifest_state == "V2_BUILDING":
            evidence_generation = current_generation + 1 if type(current_generation) is int else None
        else:
            persisted_generation = manifest_digests.get("evidence_generation")
            if type(persisted_generation) is not int or persisted_generation < 0:
                self._block(blockers, "evidence_generation_missing", "digests", status="NOT_EVALUATED")
                evidence_generation = current_generation if manifest_state == "V2_READY" and type(current_generation) is int else None
            else:
                evidence_generation = persisted_generation
                if manifest_state == "V2_READY" and persisted_generation != current_generation:
                    self._block(blockers, "evidence_generation_mismatch", "digests")
        base_ok = not blockers
        candidate = ReadinessEvidence(
            metrics=metrics,
            source_digest=calculated_source_digest,
            target_digest=calculated_target_digest,
            manifest_digest=calculated_manifest_digest,
            checkpoints=dict(checkpoints) if isinstance(checkpoints, Mapping) else {},
            validator_passed=base_ok,
            migration_id=str(_value(manifest, "migration_id", "")),
            generation=evidence_generation,
        )
        candidate_digest = candidate.digest
        expected_evidence_digest = str(manifest_digests.get("evidence_digest") or manifest_digests.get("readiness_digest") or "")
        if not expected_evidence_digest and manifest_state != "V2_BUILDING":
            self._block(blockers, "evidence_digest_missing", "digests", status="NOT_EVALUATED")
        elif expected_evidence_digest != candidate_digest:
            if expected_evidence_digest:
                self._block(blockers, "evidence_digest_mismatch", "digests")

        evidence = candidate
        if blockers and candidate.validator_passed:
            evidence = ReadinessEvidence(
                metrics=metrics,
                source_digest=candidate.source_digest,
                target_digest=candidate.target_digest,
                manifest_digest=candidate.manifest_digest,
                checkpoints=dict(candidate.checkpoints),
                validator_passed=False,
                migration_id=candidate.migration_id,
                generation=candidate.generation,
            )
        gate_result = self.gate.evaluate(evidence)
        status = "READY" if not blockers and gate_result.ready else "BLOCKED"
        transition_digests = dict(manifest_digests)
        transition_digests.update({
            "validator_passed": status == "READY",
            "checkpoints": dict(checkpoints) if isinstance(checkpoints, Mapping) else {},
            "evidence_digest": candidate_digest,
            "evidence_generation": evidence_generation,
        })
        transition_payload = {
            "migration_id": str(_value(manifest, "migration_id", "")),
            "source_digest": calculated_source_digest,
            "target_digest": calculated_target_digest,
            "manifest_digest": calculated_manifest_digest,
            "digests": transition_digests,
            "expected_generation": current_generation,
        } if manifest_state == "V2_BUILDING" else {}
        domains = {name: {"status": "PASS" if name in set(_value(audit_result, "domains", ()) or ()) and _status(audit.get("status")) == "PASS" else "BLOCKED"} for name in self.registry.names}
        domains["maintenance"] = {"status": _status(maintenance.get("status"))}
        validation_domains = {
            str(name): {"status": _status(_value(item, "status", "")), "error_count": len(_value(item, "errors", ()) or ())}
            for name, item in _mapping(validation.get("domains")).items()
        }
        native_surfaces = {
            str(name): {
                key: value
                for key, value in _mapping(item).items()
                if key in {"total", "implemented", "neutral", "neutral-read", "retired", "blocker"}
            }
            for name, item in _mapping(native.get("surfaces")).items()
        }
        diagnostics = {
            "sources": sources,
            "validation": {
                "status": _status(validation.get("status")),
                "domains": validation_domains,
                "error_count": len(validation.get("errors") or []),
            },
            "reference_audit": audit,
            "maintenance": maintenance,
            "phase4_metrics": phase4_metrics,
            "live_source": live_source,
            "native_coverage": {
                "status": native.get("status"),
                "schema": native.get("schema"),
                "registry_digest": native.get("registry_digest"),
                "counts": native.get("counts"),
                "complete": native.get("complete"),
                "production_complete": native.get("production_complete"),
                "surfaces": native_surfaces,
            },
            "digests": {
                "source": {"recorded": str(_value(manifest, "source_digest", "")), "calculated": sources.get("calculated_digest", "")},
                "target": {"recorded": str(_value(manifest, "target_digest", "")), "calculated": audit.get("epoch2_digest", "")},
                "manifest": {"recorded": recorded_manifest_digest, "calculated": calculated_manifest_digest},
                "evidence": {"recorded": expected_evidence_digest, "calculated": candidate_digest},
            },
        }
        return ReadinessEvidenceAssembly(
            status,
            evidence,
            gate_result,
            tuple(blockers),
            domains,
            diagnostics,
            candidate_digest,
            current_generation if type(current_generation) is int else None,
            transition_payload,
        )

    collect = assemble
    snapshot = assemble


__all__ = [
    "SCHEMA",
    "NATIVE_COVERAGE_SCHEMA",
    "REQUIRED_NATIVE_SURFACES",
    "REQUIRED_CHECKPOINTS",
    "EvidenceBlocker",
    "ReadinessEvidenceAssembly",
    "ReadinessEvidenceAssembler",
    "source_set_digest",
    "target_snapshot_digest",
    "manifest_snapshot_digest",
]
