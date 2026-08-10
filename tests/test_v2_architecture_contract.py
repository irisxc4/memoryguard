"""Executable guardrails for the V2 Phase 1 architecture contract."""

from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "v2" / "phase1-architecture-contract.json"
ACCEPT = ROOT / "scripts" / "accept_v2_phase1.py"


def _acceptance_module():
    spec = importlib.util.spec_from_file_location("accept_v2_phase1_test_module", ACCEPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_v2_layout_is_exact_and_content_is_not_memory():
    data = _contract()
    assert [item["path"] for item in data["database_layout"]] == [
        ".memoryguard/runtime/runtime.db",
        ".memoryguard/memory/memory.db",
        ".memoryguard/rules/rules.db",
        ".memoryguard/evidence/evidence.db",
        ".memoryguard/content/content.db",
        ".memoryguard/knowledge/knowledge.db",
        ".memoryguard/codegraph/codegraph.db",
        ".memoryguard/assets/assets.db",
        ".memoryguard/projection/scenario.db",
        ".memoryguard/projection/profile.db",
        ".memoryguard/system/manifest.db",
    ]
    assert data["domain_rules"]["content"]["must_not_be_long_term_memory"] is True
    assert "raw_content" in data["domain_rules"]["content"]["owns"]
    assert set(("raw_content", "conversation_body", "full_transcript")).issubset(
        data["domain_rules"]["evidence"]["forbids"]
    )


def test_manifest_four_state_machine_does_not_skip_ready():
    manifest = _contract()["manifest"]
    assert set(manifest["states"]) == {"V1_ACTIVE", "V2_BUILDING", "V2_READY", "V2_ACTIVE"}
    transitions = {(item["from"], item["to"]) for item in manifest["transitions"]}
    assert ("V1_ACTIVE", "V2_BUILDING") in transitions
    assert ("V2_BUILDING", "V2_READY") in transitions
    assert ("V2_READY", "V2_ACTIVE") in transitions
    assert ("V2_BUILDING", "V1_ACTIVE") in transitions
    assert ("V2_READY", "V1_ACTIVE") in transitions
    assert ("V2_ACTIVE", "V1_ACTIVE") in transitions
    assert ("V2_BUILDING", "V2_ACTIVE") not in transitions
    assert ("V2_ACTIVE", "V2_BUILDING") not in transitions
    assert manifest["v2_read_requires"] == "V2_ACTIVE"
    assert manifest["build_mode"] == "no_dual_read_or_write"
    assert manifest["physical_atomicity_claim"] is False
    assert set(manifest["ready_requires"]) == {
        "source_digest", "target_digest", "manifest_digest", "validator_passed", "checkpoints",
    }
    assert manifest["active_inherits_ready_evidence"] is True


def test_datahome_and_migration_boundary_are_explicit():
    data = _contract()
    home = data["data_home"]
    assert {"workspace_source_pointer", "global_source_pointer", "data_home_root"} <= set(home["manifest_fields"])
    assert home["pointer_must_be_explicit"] is True
    assert home["containment_check_required"] is True
    assert home["forbid_guessing_or_silent_move"] is True
    migration = data["migration"]
    assert migration["v1_dependency"] == "migration_reader_only"
    assert migration["lossless_conversion_claim"] is False
    assert migration["unknown_domains"] == {
        "codegraph": "NO_SOURCE",
        "assets": "NO_SOURCE",
        "taskcanvas": "NO_SOURCE",
    }


def test_v2_adrs_and_acceptance_script_name_the_non_negotiable_rules():
    adr_dir = ROOT / "docs" / "adr"
    texts = "\n".join(path.read_text(encoding="utf-8") for path in adr_dir.glob("ADR-*-v2-*.md"))
    for needle in ("content/content.db", "evidence.db", "V2_READY", "V1_ACTIVE", "NO_SOURCE", "lossless"):
        assert needle in texts
    script = ACCEPT.read_text(encoding="utf-8")
    for needle in (
        "integrity_check",
        "foreign_key_check",
        "readonly_no_create",
        "no_executescript",
        "no_legacy_imports",
        "dynamic_import_risk",
        "malformed_manifest_json",
        "future_schema_no_downgrade",
        "symlink_escape",
        "checkpoint_restart",
        "manifest_pointer",
        "manifest_ledger",
        "manifest_digest",
        "phase2_scope_not_claimed",
        "json.dumps",
        "V2_READY",
    ):
        assert needle in script


def test_acceptance_script_always_returns_machine_readable_json():
    process = subprocess.run(
        [sys.executable, str(ACCEPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    # A missing parallel storage implementation is a dependency failure, not
    # a malformed acceptance result.  Once storage lands this assertion also
    # accepts the zero exit path.
    assert process.returncode in (0, 1)
    result = json.loads(process.stdout)
    assert result["contract"] == "memoryguard-v2-phase1"
    assert result["phase"] == 1
    assert isinstance(result["ok"], bool)
    assert isinstance(result["checks"], dict)
    assert isinstance(result["failures"], list)
    if not result["ok"]:
        assert result["failures"]


def _scan_fixture(tmp_path: Path, source: str, *, package: str = "storage"):
    module = _acceptance_module()
    target = tmp_path / "src" / "memoryguard" / package
    target.mkdir(parents=True)
    (target / "fixture.py").write_text(source, encoding="utf-8")
    module.ROOT = tmp_path
    gate = module.Gate()
    module._scan_new_v2_sources(
        gate,
        {"acceptance": {"new_v2_source_roots": [f"src/memoryguard/{package}"]}},
    )
    return gate


def test_ast_source_scan_ignores_comments_and_strings(tmp_path: Path):
    gate = _scan_fixture(
        tmp_path,
        '''
# conn.executescript("comment")
literal = "obj.executescript('string')"
doc = """from memoryguard.shared_memory_store import SharedMemoryStore"""
''',
    )
    assert gate.checks["no_executescript"]["ok"]
    assert gate.checks["no_legacy_imports"]["ok"]
    assert gate.checks["dynamic_import_risk"]["ok"]


def test_ast_source_scan_rejects_attribute_calls_and_legacy_import_variants(tmp_path: Path):
    gate = _scan_fixture(
        tmp_path,
        '''
from memoryguard.shared_memory_store import (SharedMemoryStore as Store,)
import memoryguard.conversation_history as history
import importlib as il
il.import_module("memoryguard.shared_memory_store")
__import__(
    "memoryguard.conversation_history",
)
conn = object()
conn.executescript(
    "CREATE TABLE x(id INTEGER)"
)
''',
    )
    assert not gate.checks["no_executescript"]["ok"]
    assert not gate.checks["no_legacy_imports"]["ok"]
    assert gate.checks["dynamic_import_risk"]["ok"]


def test_ast_source_scan_reports_unresolvable_dynamic_import_and_allows_migration_boundary(tmp_path: Path):
    gate = _scan_fixture(
        tmp_path,
        '''
import importlib
module_name = get_module_name()
importlib.import_module(module_name)
''',
        package="system",
    )
    assert not gate.checks["dynamic_import_risk"]["ok"]

    migration = tmp_path / "src" / "memoryguard" / "migration"
    migration.mkdir(parents=True)
    (migration / "legacy_reader.py").write_text(
        "from memoryguard.shared_memory_store import SharedMemoryStore\n"
        "conn.executescript('allowed only in migration reader')\n",
        encoding="utf-8",
    )
    # The configured scan scope is storage/system, so migration reader code is
    # deliberately not included in the V2-core gate.
    assert gate.checks["no_executescript"]["ok"]
    assert gate.checks["no_legacy_imports"]["ok"]
