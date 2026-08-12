from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

from memoryguard.cutover_v2 import ReadinessGate
from memoryguard.cutover_v2.evidence_assembler import (
    REQUIRED_CHECKPOINTS,
    ReadinessEvidenceAssembler,
    _bind_native_coverage_for_test,
)
from memoryguard.cutover_v2.readiness import stable_digest
from memoryguard.cutover_v2.surfaces import (
    CLI_COMMAND_NAMES,
    GUI_METHOD_NAMES,
    GUI_MUTATION_NAMES,
    GUI_OPERATION_SPECS,
    MCP_MUTATION_NAMES,
    MCP_TOOL_NAMES,
)
from memoryguard.maintenance_v2.registry import DEFAULT_REGISTRY


@dataclass
class _Page:
    domain: str
    table: str
    rows: tuple[dict[str, str], ...]
    fingerprint: str


@dataclass
class _AuditResult:
    status: str
    domains: tuple[str, ...]
    schema_fingerprints: dict[str, str]
    registry_digest: str
    manifest_generation: int
    pages: tuple[_Page, ...]
    blockers: tuple[Any, ...] = ()
    candidates: tuple[str, ...] = ()

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "domains": list(self.domains),
            "schema_fingerprints": dict(self.schema_fingerprints),
            "registry_digest": self.registry_digest,
            "manifest_generation": self.manifest_generation,
            "blockers": [],
        }


class _Audit:
    def __init__(self, result: _AuditResult) -> None:
        self.result = result
        self.calls = 0

    def audit(self, **_kwargs: Any) -> _AuditResult:
        self.calls += 1
        return self.result


class _Validator:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result

    def validate(self) -> dict[str, Any]:
        return self.result


class _CoverageProvider:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value

    def coverage(self) -> dict[str, Any]:
        return self.value


class _Manifest:
    state = "V2_BUILDING"
    generation = 7
    migration_id = "migration-fixture"
    workspace_source_pointer = "workspace"
    global_source_pointer = "NOT_CONFIGURED"
    data_home_root = "NOT_CONFIGURED"

    def __init__(self, checkpoints: dict[str, Any]) -> None:
        self.checkpoints = checkpoints
        self.source_digest = ""
        self.target_digest = ""
        self.manifest_digest = ""
        self.digests: dict[str, Any] = {}
        self.transition_called = False

    def current(self) -> "_Manifest":
        return self

    def transition(self, *_args: Any, **_kwargs: Any) -> None:
        self.transition_called = True
        raise AssertionError("assembler must never transition manifest")


def _refresh_native(value: dict[str, Any]) -> dict[str, Any]:
    totals = {"implemented": 0, "neutral-read": 0, "retired": 0, "blocker": 0}
    canonical: dict[str, Any] = {}
    for surface, item in value["surfaces"].items():
        entries = item["entries"]
        counts = {status: sum(entry["status"] == status for entry in entries) for status in totals}
        item.update({"total": len(entries), **counts})
        for status, count in counts.items():
            totals[status] += count
        canonical[surface] = sorted(entries, key=lambda entry: entry["name"])
    digest = stable_digest(canonical)
    value["registry_digest"] = digest
    value["coverage_digest"] = digest
    value["counts"] = {"total": sum(totals.values()), **totals}
    value["complete"] = totals["blocker"] == 0
    value["production_complete"] = value["complete"] and totals["neutral-read"] == 0
    return value


def _native(status: str = "implemented", *, omit: str = "") -> dict[str, Any]:
    expected = {
        "mcp": MCP_TOOL_NAMES,
        "gui": GUI_METHOD_NAMES,
        "cli": CLI_COMMAND_NAMES,
        "hook": frozenset({"bootstrap_hook"}),
    }
    surfaces: dict[str, Any] = {}
    for surface in ("mcp", "gui", "cli", "hook"):
        if surface == omit:
            continue
        entry_status = status if surface == "hook" else "implemented"
        entries = []
        for name in sorted(expected[surface]):
            mutation = name in MCP_MUTATION_NAMES if surface == "mcp" else False
            canonical_name = name
            domain = surface
            execution = "sync"
            if surface == "gui":
                mutation = name in GUI_MUTATION_NAMES
                operation = GUI_OPERATION_SPECS[name]
                canonical_name = operation.canonical_name
                domain = operation.domain
                execution = operation.execution
            entries.append({
                "name": name,
                "status": entry_status,
                "handler": f"fixture_{surface}",
                "mutation": mutation,
                "reason": (
                    "fixture blocker" if entry_status == "blocker"
                    else ("fixture retired" if entry_status == "retired" else "")
                ),
                "canonical_name": canonical_name,
                "domain": domain,
                "execution": execution,
            })
        surfaces[surface] = {"entries": entries}
    return _refresh_native({
        "schema": "v2-native-coverage-1",
        "surfaces": surfaces,
    })


def _fixture(tmp_path: Path) -> tuple[ReadinessEvidenceAssembler, _Manifest, dict[str, Any]]:
    hashes = {"history": "1" * 64}
    checkpoints = {key: {"ok": True, "status": "PASS"} for key in REQUIRED_CHECKPOINTS}
    checkpoints["phase2_sources"] = {"hashes": dict(hashes), "pointers": {"workspace": str(tmp_path.resolve())}}
    manifest = _Manifest(checkpoints)
    manifest.workspace_source_pointer = str(tmp_path.resolve())
    validation = {
        "status": "PASS",
        "ok": True,
        "domains": {
            "storage": {"status": "PASS", "metrics": {}},
            "content": {"status": "PASS", "metrics": {"loss": 0}},
            "memory": {"status": "PASS", "metrics": {"loss": 0, "evidence_orphan": 0}},
            "rules": {"status": "PASS", "metrics": {"loss": 0, "binding_identity_multiset_diff": 0, "auto_scope_expansion": 0}},
            "evidence": {"status": "PASS", "metrics": {"evidence_orphan": 0}},
        },
        "metrics": {
            "content": {"loss": 0},
            "memory": {"loss": 0, "evidence_orphan": 0},
            "rules": {"loss": 0, "binding_identity_multiset_diff": 0, "auto_scope_expansion": 0},
            "evidence": {"evidence_orphan": 0},
        },
        "errors": [],
        "source_hashes": dict(hashes),
        "expected_source_hashes": dict(hashes),
        "source_status": {"history": "READY", "knowledge": "NO_SOURCE"},
    }
    fingerprints = {name: f"fingerprint-{name}" for name in DEFAULT_REGISTRY.names}
    pages = tuple(
        _Page(name, "fixture", ({"row_hash": f"row-{name}"},), fingerprints[name])
        for name in DEFAULT_REGISTRY.names
    )
    audit_result = _AuditResult("PASS", DEFAULT_REGISTRY.names, fingerprints, DEFAULT_REGISTRY.digest, manifest.generation, pages)
    maintenance = {"status": "PASS", "integrity": "ok", "foreign_key_errors": 0, "schema_fingerprint": "maintenance-fingerprint"}
    phase4 = {
        "ok": True,
        "mandatory_equivalence": True,
        "scope_leak_count": 0,
        "leak": 0,
        "recall_at_k": 3,
        "v1_recall_at_k": 3,
        "context_tokens": {"v1": 20, "v2": 10},
        "deterministic": True,
    }
    assembler = ReadinessEvidenceAssembler(
        tmp_path,
        phase4_evidence=phase4,
        native_coverage=_bind_native_coverage_for_test(_native()),
        validator=_Validator(validation),
        reference_audit=_Audit(audit_result),
        manifest_manager=manifest,
        maintenance_provider=lambda: maintenance,
    )
    assert assembler.assemble().ready
    return assembler, manifest, phase4


def test_production_readiness_requires_frozen_sources(tmp_path: Path) -> None:
    assembler, _manifest, _phase4 = _fixture(tmp_path)
    assembler.require_frozen_sources = True
    assembler.live_source_verifier = lambda *args, **kwargs: {
        "status": "PASS", "activation_safe": True, "checked": 1, "snapshot_digest": "f" * 64,
    }
    result = assembler.assemble()
    assert not result.ready
    assert "frozen_source_snapshot_required" in {item.code for item in result.blockers}


def test_production_readiness_rechecks_live_source_and_blocks_drift(tmp_path: Path) -> None:
    assembler, manifest, _phase4 = _fixture(tmp_path)
    snapshot_root = tmp_path / ".memoryguard" / "migration-backups" / manifest.migration_id / "source-snapshot"
    manifest.checkpoints["phase2_sources"]["snapshot"] = {
        "mode": "frozen",
        "workspace": str(snapshot_root / "workspace"),
        "data_home": "NOT_CONFIGURED",
    }
    assembler.require_frozen_sources = True
    assembler.live_source_verifier = lambda *args, **kwargs: {
        "status": "DRIFT", "activation_safe": False, "checked": 1, "snapshot_digest": "f" * 64,
    }
    blocked = assembler.assemble()
    assert not blocked.ready
    assert "live_source_drift_detected" in {item.code for item in blocked.blockers}
    assembler.live_source_verifier = lambda *args, **kwargs: {
        "status": "PASS", "activation_safe": True, "checked": 1, "snapshot_digest": "f" * 64,
    }
    assert assembler.assemble().ready


def test_complete_assembly_is_direct_gate_input_and_never_transitions(tmp_path: Path) -> None:
    assembler, manifest, _phase4 = _fixture(tmp_path)
    result = assembler.assemble()

    assert result.ready
    assert ReadinessGate().evaluate(result.evidence).ready
    assert set(result.domains) == set(DEFAULT_REGISTRY.names) | {"maintenance"}
    assert result.diagnostics["native_coverage"]["status"] == "PASS"
    assert result.expected_generation == 7
    assert result.transition_payload["expected_generation"] == 7
    assert result.evidence.generation == 8
    assert str(tmp_path.resolve()) not in json.dumps(result.to_public_dict(), ensure_ascii=False)
    assert manifest.transition_called is False


def test_building_payload_replays_in_ready_and_active_but_tampering_blocks(tmp_path: Path) -> None:
    assembler, manifest, _phase4 = _fixture(tmp_path)
    building = assembler.assemble()
    payload = dict(building.transition_payload)

    manifest.state = "V2_READY"
    manifest.generation += 1
    manifest.source_digest = payload["source_digest"]
    manifest.target_digest = payload["target_digest"]
    manifest.manifest_digest = payload["manifest_digest"]
    manifest.digests = dict(payload["digests"])
    assembler.reference_audit.result.manifest_generation = manifest.generation
    assert assembler.assemble().ready

    manifest.source_digest = "tampered"
    assert "source_digest_mismatch" in {item.code for item in assembler.assemble().blockers}
    manifest.source_digest = payload["source_digest"]

    manifest.state = "V2_ACTIVE"
    manifest.generation += 1
    assembler.reference_audit.result.manifest_generation = manifest.generation
    assert assembler.assemble().ready
    manifest.digests["evidence_digest"] = "tampered"
    assert "evidence_digest_mismatch" in {item.code for item in assembler.assemble().blockers}


def test_missing_phase4_metric_stays_not_evaluated_never_zero(tmp_path: Path) -> None:
    assembler, _manifest, phase4 = _fixture(tmp_path)
    phase4.pop("leak")

    result = assembler.assemble()

    assert not result.ready
    assert result.evidence.metrics["leak"] == "NOT_EVALUATED"
    assert "phase4_metrics_missing" in {item.code for item in result.blockers}


def test_source_key_hash_set_must_match_exactly(tmp_path: Path) -> None:
    assembler, _manifest, _phase4 = _fixture(tmp_path)
    assembler.validator.result["source_hashes"]["unexpected"] = "2" * 64

    result = assembler.assemble()

    blocker = next(item for item in result.blockers if item.code == "source_key_hash_set_mismatch")
    assert blocker.detail["unexpected"] == ["unexpected"]
    assert not result.ready


def test_reference_registry_must_include_skills_and_maintenance_proof(tmp_path: Path) -> None:
    assembler, _manifest, _phase4 = _fixture(tmp_path)
    audit_result = assembler.reference_audit.result
    audit_result.domains = tuple(name for name in audit_result.domains if name != "skills")
    audit_result.schema_fingerprints.pop("skills")
    assembler.maintenance_provider = lambda: {"status": "BLOCKED", "integrity": "NOT_EVALUATED", "foreign_key_errors": "NOT_EVALUATED", "schema_fingerprint": ""}

    result = assembler.assemble()
    codes = {item.code for item in result.blockers}

    assert "reference_domains_incomplete" in codes
    assert "schema_fingerprints_incomplete" in codes
    assert "maintenance_blocked" in codes
    assert not result.ready


def test_native_coverage_missing_or_blocker_fails_closed(tmp_path: Path) -> None:
    assembler, _manifest, _phase4 = _fixture(tmp_path)
    assembler.native_coverage = None
    missing = assembler.assemble()
    assert missing.evidence.metrics["native_coverage"] == "NOT_EVALUATED"
    assert "native_coverage_missing" in {item.code for item in missing.blockers}

    assembler.native_coverage = _bind_native_coverage_for_test(_native("blocker"))
    blocked = assembler.assemble()
    assert "native_operation_blocked" in {item.code for item in blocked.blockers}
    assert not blocked.ready


def test_injected_native_provider_accepts_final_neutral_read_schema(tmp_path: Path) -> None:
    assembler, _manifest, _phase4 = _fixture(tmp_path)
    assembler.native_coverage = _bind_native_coverage_for_test(_native("neutral-read"))

    result = assembler.assemble()

    assert not result.ready
    assert "native_production_incomplete" in {item.code for item in result.blockers}
    assert result.diagnostics["native_coverage"]["status"] == "BLOCKED"


def test_native_mapping_and_ordinary_provider_are_diagnostic_only(tmp_path: Path) -> None:
    assembler, _manifest, _phase4 = _fixture(tmp_path)
    for provider in (_native(), _CoverageProvider(_native())):
        assembler.native_coverage = provider
        result = assembler.assemble()
        assert not result.ready
        assert "native_coverage_untrusted" in {item.code for item in result.blockers}


def test_native_one_entry_per_surface_forgery_is_rejected(tmp_path: Path) -> None:
    assembler, _manifest, _phase4 = _fixture(tmp_path)
    forged = _native()
    for item in forged["surfaces"].values():
        item["entries"] = item["entries"][:1]
    _refresh_native(forged)
    assembler.native_coverage = _bind_native_coverage_for_test(forged)
    assembler.expected_native_registry_digest = forged["registry_digest"]

    result = assembler.assemble()

    assert not result.ready
    assert "native_operation_set_mismatch" in {item.code for item in result.blockers}


def test_native_exact_names_reject_missing_extra_and_duplicate(tmp_path: Path) -> None:
    assembler, _manifest, _phase4 = _fixture(tmp_path)
    attacks: list[tuple[dict[str, Any], str]] = []

    missing = _native()
    missing["surfaces"]["mcp"]["entries"].pop()
    attacks.append((_refresh_native(missing), "native_operation_set_mismatch"))

    extra = _native()
    entry = dict(extra["surfaces"]["gui"]["entries"][0])
    entry["name"] = "forged_gui_operation"
    extra["surfaces"]["gui"]["entries"].append(entry)
    attacks.append((_refresh_native(extra), "native_operation_set_mismatch"))

    duplicate = _native()
    duplicate["surfaces"]["cli"]["entries"].append(dict(duplicate["surfaces"]["cli"]["entries"][0]))
    attacks.append((_refresh_native(duplicate), "native_operation_names_duplicate"))

    for coverage, code in attacks:
        assembler.native_coverage = _bind_native_coverage_for_test(coverage)
        result = assembler.assemble()
        assert not result.ready
        assert code in {item.code for item in result.blockers}


def test_native_rejects_mutation_count_and_digest_forgery(tmp_path: Path) -> None:
    assembler, _manifest, _phase4 = _fixture(tmp_path)

    mutation = _native()
    target = next(entry for entry in mutation["surfaces"]["mcp"]["entries"] if entry["name"] in MCP_MUTATION_NAMES)
    target["mutation"] = False
    _refresh_native(mutation)
    assembler.native_coverage = _bind_native_coverage_for_test(mutation)
    assert "native_mutation_classification_mismatch" in {item.code for item in assembler.assemble().blockers}

    counts = _native()
    counts["surfaces"]["gui"]["total"] -= 1
    assembler.native_coverage = _bind_native_coverage_for_test(counts)
    assert "native_surface_counts_mismatch" in {item.code for item in assembler.assemble().blockers}

    digest = _native()
    digest["registry_digest"] = "a" * 64
    digest["coverage_digest"] = "a" * 64
    assembler.native_coverage = _bind_native_coverage_for_test(digest)
    codes = {item.code for item in assembler.assemble().blockers}
    assert {"native_registry_digest_mismatch", "native_coverage_digest_mismatch"} <= codes


def test_machine_script_is_read_only_on_empty_workspace(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "accept_v2_readiness.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--workspace", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)

    assert completed.returncode == 1
    assert report["status"] == "BLOCKED"
    blocker_codes = {item.get("code") for item in report.get("blockers", [])}
    assert "phase4_missing" not in blocker_codes
    assert "phase4_not_passed" not in blocker_codes
    assert not (tmp_path / ".memoryguard").exists()


def test_phase4_script_and_importable_evidence_share_one_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "accept_v2_phase4.py"
    completed = subprocess.run(
        [sys.executable, str(script)],
        check=False,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    from memoryguard.runtime_v2.phase4_acceptance import phase4_acceptance_evidence

    assert completed.returncode == 0
    assert report == phase4_acceptance_evidence()
    assert report["status"] == "PASS"
    assert report["mandatory_equivalence"] is True
    assert report["scope_leak_count"] == 0
    assert report["deterministic"] is True
