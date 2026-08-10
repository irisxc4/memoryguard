from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from memoryguard.maintenance_v2.api import MaintenanceV2Api


def _fully_initialized_workspace(root: Path) -> None:
    from memoryguard.assets_v2.store import AssetStore
    from memoryguard.codegraph_v2.store import CodeGraphStore
    from memoryguard.content.store import ContentStore
    from memoryguard.evidence.store import EvidenceStore
    from memoryguard.memory.store import MemoryAtomStore
    from memoryguard.projection_v2.store import ProjectionStore
    from memoryguard.rules.v2_store import RuleV2Store
    from memoryguard.runtime_v2.working_memory import RuntimeStore
    from memoryguard.skills_v2.store import SkillStore
    from memoryguard.storage.layout import WorkspaceV2Layout
    from memoryguard.storage.schema import initialize_database
    from memoryguard.system.manifest import ManifestManager, ManifestState

    layout = WorkspaceV2Layout(root)
    layout.ensure_dirs()
    for domain, paths in layout.databases.items():
        for path in paths:
            try:
                initialize_database(path, domain, layout=layout)
            except Exception:
                pass
    for store_cls in (RuntimeStore, MemoryAtomStore, RuleV2Store, EvidenceStore, ContentStore, CodeGraphStore, AssetStore, SkillStore):
        store_cls(root)
    ProjectionStore(root)
    manager = ManifestManager(root)
    manager.transition(ManifestState.V2_BUILDING, migration_id="overview-test")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="source",
        target_digest="target",
        manifest_digest="manifest",
        digests={"validator_passed": True, "checkpoints": {"overview": True}},
    )
    manager.transition(ManifestState.V2_ACTIVE)


def test_storage_overview_missing_workspace_is_blocked_without_creation(tmp_path: Path):
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    result = MaintenanceV2Api(tmp_path).get_storage_overview()
    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))

    assert result["status"] == "blocked"
    assert {item["code"] for item in result["blockers"]} >= {"missing_database", "manifest_not_ready"}
    assert before == after == []
    assert all(set(item) <= {"domain", "status", "bytes", "schema_version", "health", "counters"} for item in result["domains"])
    assert all("path" not in item and "body" not in item for item in result["domains"])


def test_storage_overview_digest_is_stable_and_public_only(tmp_path: Path):
    first = MaintenanceV2Api(tmp_path).get_storage_overview()
    second = MaintenanceV2Api(tmp_path).get_storage_overview()

    assert first["digest"] == second["digest"]
    payload = dict(first)
    digest = payload.pop("digest")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert digest == hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    serialized = json.dumps(first, ensure_ascii=False)
    assert str(tmp_path) not in serialized
    assert all("scope" not in json.dumps(item, ensure_ascii=False) for item in first["domains"])
    assert all("secret" not in json.dumps(item, ensure_ascii=False) for item in first["domains"])


def test_storage_overview_ready_for_fully_initialized_v2_workspace(tmp_path: Path):
    _fully_initialized_workspace(tmp_path)
    def sidecars() -> dict[str, tuple[bool, bytes]]:
        result: dict[str, tuple[bool, bytes]] = {}
        for path in tmp_path.rglob("*.db"):
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = path.with_name(path.name + suffix)
                result[str(sidecar.relative_to(tmp_path))] = (sidecar.exists(), sidecar.read_bytes() if sidecar.is_file() else b"")
        return result

    before = sidecars()
    result = MaintenanceV2Api(tmp_path).get_storage_overview()
    after = sidecars()

    assert result["status"] == "ready"
    assert result["blockers"] == []
    assert result["manifest"]["status"] == "V2_ACTIVE"
    assert result["manifest"]["generation"] >= 1
    assert [item["domain"] for item in result["domains"]] == [
        "runtime", "memory", "rules", "evidence", "content", "knowledge",
        "codegraph", "assets", "scenario", "profile", "system", "skills",
    ]
    assert all(item["status"] == "ready" for item in result["domains"])
    assert all(item["schema_version"] is not None for item in result["domains"])
    assert after == before, {key: (before.get(key), after.get(key)) for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)}


def test_storage_overview_future_schema_fails_closed(tmp_path: Path):
    _fully_initialized_workspace(tmp_path)
    runtime_db = tmp_path / ".memoryguard" / "runtime" / "runtime.db"
    with sqlite3.connect(runtime_db) as conn:
        conn.execute("UPDATE schema_meta SET version=99, marker='future-runtime'")
        conn.execute("PRAGMA user_version=99")

    result = MaintenanceV2Api(tmp_path).get_storage_overview()

    assert result["status"] == "blocked"
    assert {item["code"] for item in result["blockers"] if item["domain"] == "runtime"} >= {"future_schema"}
    runtime = next(item for item in result["domains"] if item["domain"] == "runtime")
    assert runtime["status"] == "blocked"
    assert "path" not in runtime and "row_counts" not in runtime
