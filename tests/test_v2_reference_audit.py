from __future__ import annotations

import sqlite3
from pathlib import Path
import os
from dataclasses import replace

import pytest

from memoryguard.maintenance_v2.adapters import CursorError, ReadOnlyAdapterError, SQLiteReadOnlyAdapter
from memoryguard.maintenance_v2.reference_audit import ReferenceAudit, _METADATA_MARKERS
from memoryguard.maintenance_v2.registry import DEFAULT_REGISTRY, DomainRegistry, TableSpec


def _fixture(root: Path, *, omit: str | None = None) -> Path:
    for spec in DEFAULT_REGISTRY:
        path = DEFAULT_REGISTRY.path_for(root, spec.name)
        if spec.name == omit:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        user_version = spec.supported_user_versions[0]
        with sqlite3.connect(path) as conn:
            conn.execute(f"PRAGMA user_version={user_version}")
            for table, table_spec in spec.tables.items():
                extra = {
                    "task_runs": {"run_id", "task_type", "state"},
                    "asset_unknown_ledger": {"unknown_id", "source_domain", "source_ref", "field", "value", "status", "created_at"},
                    "content_blobs": {"blob_id"}, "content_holds": {"hold_id", "blob_id", "active"},
                    "skill_asset_refs": {"ref_id", "version_id", "asset_id", "path", "digest", "asset_kind"},
                    "domain_outbox": {"event_id", "sequence", "event_type", "aggregate_id", "payload_json", "status"},
                    "skill_versions": {"version_id", "skill_id", "version"},
                }.get(table, set())
                numeric = {"generation", "version", "sequence", "last_sequence", "active", "user_version"}
                columns = ", ".join(f'"{column}" {"INTEGER" if column in numeric else "TEXT"}' for column in sorted(table_spec.required_columns or extra or {"dummy_id"}))
                conn.execute(f'CREATE TABLE "{table}" ({columns})')
            for metadata in ("schema_meta", f"{spec.name}_schema_meta", "asset_schema_meta", "codegraph_schema_meta", "runtime_v2_schema_meta", "content_schema_meta", "projection_schema_meta", "gui_control_schema_meta"):
                if metadata not in spec.tables:
                    continue
                expected = next(((marker, marker_version) for table, marker, marker_version in _METADATA_MARKERS[spec.name] if table == metadata), None)
                if expected is None:
                    continue
                marker, marker_version = expected
                columns = set(spec.tables[metadata].required_columns)
                if {"domain", "version", "marker", "updated_at"} <= columns:
                    conn.execute(f'INSERT INTO "{metadata}"(domain,version,marker,updated_at) VALUES(?,?,?,?)', (spec.name, marker_version, marker, ""))
                elif {"schema_id", "version", "marker", "updated_at"} <= columns:
                    conn.execute(f'INSERT INTO "{metadata}"(schema_id,version,marker,updated_at) VALUES(?,?,?,?)', ("schema", marker_version, marker, ""))
                elif {"key", "value"} <= columns:
                    conn.execute(f'INSERT INTO "{metadata}"(key,value) VALUES(?,?)', ("version", str(marker_version)))
            if spec.name == "system":
                conn.execute("INSERT INTO manifest(manifest_id,state,generation) VALUES('fixture','V2_READY',0)")
    return root


def test_registry_has_all_twelve_domains_and_skills(tmp_path: Path):
    assert DEFAULT_REGISTRY.names == ("runtime", "memory", "rules", "evidence", "content", "knowledge", "codegraph", "assets", "scenario", "profile", "system", "skills")
    assert "skills" in DEFAULT_REGISTRY
    assert DEFAULT_REGISTRY.digest
    assert all(spec.tables[table].columns is not None and spec.tables[table].required_columns == spec.tables[table].columns for spec in DEFAULT_REGISTRY for table in spec.tables)
    with pytest.raises(ValueError):
        TableSpec("empty", frozenset(), frozenset())
    with pytest.raises(ValueError):
        TableSpec("widened", frozenset({"id"}), frozenset({"id", "attacker"}))


def test_missing_database_is_blocked_without_creating_anything(tmp_path: Path):
    result = ReferenceAudit(tmp_path).audit()
    assert result.status == "BLOCKED"
    assert {item.code for item in result.blockers} == {"missing_database"}
    assert not (tmp_path / ".memoryguard").exists()


def test_pagination_and_cursor_binding(tmp_path: Path):
    _fixture(tmp_path)
    path = DEFAULT_REGISTRY.path_for(tmp_path, "runtime")
    spec = DEFAULT_REGISTRY["runtime"]
    adapter = SQLiteReadOnlyAdapter(path, spec, domain="runtime")
    page = adapter.page("task_runs", limit=1)
    assert page.done
    with pytest.raises(CursorError):
        adapter.page("task_runs", cursor="tampered")


def test_unknown_table_is_blocker(tmp_path: Path):
    _fixture(tmp_path)
    path = DEFAULT_REGISTRY.path_for(tmp_path, "skills")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE unknown_authoritative(value TEXT)")
    result = ReferenceAudit(tmp_path).audit()
    assert any(blocker.code == "unknown_authoritative_table" for blocker in result.blockers)


def test_two_epoch_intersection_and_no_sweep(tmp_path: Path):
    _fixture(tmp_path)
    first = ReferenceAudit(tmp_path).audit()
    second = ReferenceAudit(tmp_path).audit(previous=first)
    assert second.epoch_candidates == (first.candidates, second.candidates)
    assert second.sweep == {"capability": False, "reason": "hold_first_not_proven", "deleted": 0}


def test_partial_schema_does_not_self_heal_or_change_source(tmp_path: Path):
    _fixture(tmp_path)
    path = DEFAULT_REGISTRY.path_for(tmp_path, "skills")
    before = path.read_bytes()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE skill_versions")
    result = ReferenceAudit(tmp_path).audit()
    assert any(item.code == "partial_schema" for item in result.blockers)
    assert path.read_bytes() != before  # intentional fixture mutation only
    after = path.stat().st_mtime_ns
    ReferenceAudit(tmp_path).audit()
    assert path.stat().st_mtime_ns == after


def test_unknown_column_and_unknown_ledger_are_blockers(tmp_path: Path):
    _fixture(tmp_path)
    with sqlite3.connect(DEFAULT_REGISTRY.path_for(tmp_path, "skills")) as conn:
        conn.execute("ALTER TABLE skill_versions ADD COLUMN attacker_column TEXT")
    with sqlite3.connect(DEFAULT_REGISTRY.path_for(tmp_path, "content")) as conn:
        conn.execute("ALTER TABLE content_blobs ADD COLUMN attacker_column TEXT")
    with sqlite3.connect(DEFAULT_REGISTRY.path_for(tmp_path, "assets")) as conn:
        conn.execute("INSERT INTO asset_unknown_ledger(unknown_id,source_domain,source_ref,field,value,status,created_at) VALUES('u','x','r','f','v','BLOCKED','')")
    result = ReferenceAudit(tmp_path).audit()
    codes = {item.code for item in result.blockers}
    assert "unknown_authoritative_column" in codes
    assert "unknown_ledger" in codes


def test_exact_non_core_columns_are_blocked(tmp_path: Path):
    _fixture(tmp_path)
    for domain, table in (("rules", "rule_definitions"), ("runtime", "task_runs"), ("codegraph", "symbols")):
        with sqlite3.connect(DEFAULT_REGISTRY.path_for(tmp_path, domain)) as conn:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN evil_col TEXT')
    result = ReferenceAudit(tmp_path).audit()
    assert sum(item.code == "unknown_authoritative_column" for item in result.blockers) >= 3
    assert result.candidates == ()


def test_pending_and_malformed_outbox_are_blockers(tmp_path: Path):
    _fixture(tmp_path)
    with sqlite3.connect(DEFAULT_REGISTRY.path_for(tmp_path, "skills")) as conn:
        conn.execute("INSERT INTO domain_outbox(event_id,sequence,event_type,aggregate_id,payload_json,status) VALUES('e',1,'x','a','{broken','pending')")
    result = ReferenceAudit(tmp_path).audit()
    codes = {item.code for item in result.blockers}
    assert "unconsumed_outbox" in codes
    assert "malformed_authoritative_json" in codes


def test_rules_consumed_at_outbox_is_fail_closed(tmp_path: Path):
    _fixture(tmp_path)
    with sqlite3.connect(DEFAULT_REGISTRY.path_for(tmp_path, "rules")) as conn:
        conn.execute("INSERT INTO rule_domain_outbox(event_id,payload_json,consumed_at) VALUES('pending-rule','{}','')")
    result = ReferenceAudit(tmp_path).audit()
    assert any(item.code == "unconsumed_outbox" and item.table == "rule_domain_outbox" for item in result.blockers)
    assert result.candidates == ()


def test_integrity_fk_and_dangling_logical_reference(tmp_path: Path):
    _fixture(tmp_path)
    with sqlite3.connect(DEFAULT_REGISTRY.path_for(tmp_path, "skills")) as conn:
        conn.execute("INSERT INTO skill_asset_refs(ref_id,version_id,asset_id,path,digest,asset_kind) VALUES('r','v','missing-asset','','','')")
    # A foreign-key violation is independently reported even if another
    # blocker already prevents the sweep.
    with sqlite3.connect(DEFAULT_REGISTRY.path_for(tmp_path, "content")) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("CREATE TABLE fk_parent(id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE fk_child(id TEXT, parent_id TEXT, FOREIGN KEY(parent_id) REFERENCES fk_parent(id))")
        conn.execute("INSERT INTO fk_child VALUES('c','missing')")
    result = ReferenceAudit(tmp_path).audit()
    codes = {item.code for item in result.blockers}
    assert "dangling_logical_reference" in codes
    assert "foreign_key_check" in codes or "unknown_authoritative_table" in codes


def test_manifest_and_schema_drift_block_epoch_two(tmp_path: Path):
    _fixture(tmp_path)
    system = DEFAULT_REGISTRY.path_for(tmp_path, "system")
    with sqlite3.connect(system) as conn:
        conn.execute("INSERT INTO manifest(manifest_id,state,generation) VALUES('m','V2_READY',1)")
    first = ReferenceAudit(tmp_path).audit()
    with sqlite3.connect(system) as conn:
        conn.execute("INSERT INTO manifest(manifest_id,state,generation) VALUES('m2','V2_READY',2)")
    with sqlite3.connect(DEFAULT_REGISTRY.path_for(tmp_path, "skills")) as conn:
        conn.execute("ALTER TABLE skill_versions ADD COLUMN drift_column TEXT")
    second = ReferenceAudit(tmp_path).audit(previous=first)
    codes = {item.code for item in second.blockers}
    assert "manifest_generation_drift" in codes
    assert "schema_drift" in codes or "unknown_authoritative_column" in codes


def test_registry_drift_blocks_epoch_two(tmp_path: Path):
    _fixture(tmp_path)
    first = ReferenceAudit(tmp_path).audit()
    changed = [replace(spec, marker=spec.marker + ("attacker-marker",)) if spec.name == "runtime" else spec for spec in DEFAULT_REGISTRY]
    second = ReferenceAudit(tmp_path, registry=DomainRegistry(changed)).audit(previous=first)
    assert any(item.code == "registry_drift" for item in second.blockers)
    assert second.candidates == ()


def test_manifest_generation_must_be_present_and_strict_integer(tmp_path: Path):
    _fixture(tmp_path)
    system = DEFAULT_REGISTRY.path_for(tmp_path, "system")
    with sqlite3.connect(system) as conn:
        conn.execute("DELETE FROM manifest")
    assert any(item.code == "manifest_generation_unavailable" for item in ReferenceAudit(tmp_path).audit().blockers)
    with sqlite3.connect(system) as conn:
        conn.execute("INSERT INTO manifest(manifest_id,state,generation) VALUES('bad','V2_READY','1.5')")
    assert any(item.code == "manifest_generation_unavailable" for item in ReferenceAudit(tmp_path).audit().blockers)


def test_explicit_reference_target_columns_and_asset_version_discriminator(tmp_path: Path):
    _fixture(tmp_path)
    codegraph = DEFAULT_REGISTRY.path_for(tmp_path, "codegraph")
    with sqlite3.connect(codegraph) as conn:
        conn.execute("INSERT INTO symbols(symbol_id) VALUES('symbol-ok')")
        conn.execute("INSERT INTO edges(edge_id,from_id,to_id) VALUES('edge-ok','symbol-ok','symbol-ok')")
    assets = DEFAULT_REGISTRY.path_for(tmp_path, "assets")
    with sqlite3.connect(assets) as conn:
        conn.execute("INSERT INTO assets(asset_id) VALUES('asset-ok')")
        conn.execute("INSERT INTO asset_versions(version_id,asset_id) VALUES('version-ok','asset-ok')")
        conn.execute("INSERT INTO asset_references(reference_id,asset_id,reference_kind,target_id) VALUES('ref-ok','asset-ok','asset_version','version-ok')")
    content = DEFAULT_REGISTRY.path_for(tmp_path, "content")
    with sqlite3.connect(content) as conn:
        conn.execute("INSERT INTO content_blobs(blob_id) VALUES('knowledge-blob')")
        conn.execute("INSERT INTO knowledge_records(record_id,content_blob_id) VALUES('knowledge-record','knowledge-blob')")
    result = ReferenceAudit(tmp_path).audit()
    assert not [item for item in result.blockers if item.code == "dangling_logical_reference" and item.detail.get("target_id") in {"symbol-ok", "version-ok"}]
    assert "knowledge-blob" not in result.candidates


def test_more_than_two_pages_are_complete_and_epoch_hold_removes_candidate(tmp_path: Path):
    _fixture(tmp_path)
    runtime = DEFAULT_REGISTRY.path_for(tmp_path, "runtime")
    with sqlite3.connect(runtime) as conn:
        for index in range(7):
            conn.execute("INSERT INTO task_runs(run_id,task_type,state) VALUES(?,?,?)", (f"run-{index}", "audit", "succeeded"))
    content = DEFAULT_REGISTRY.path_for(tmp_path, "content")
    with sqlite3.connect(content) as conn:
        conn.execute("INSERT INTO content_blobs(blob_id) VALUES('blob-hold')")
        conn.execute("INSERT INTO content_blobs(blob_id) VALUES('blob-released')")
        conn.execute("INSERT INTO content_holds(hold_id,blob_id,active) VALUES('released','blob-released',0)")
    first = ReferenceAudit(tmp_path, page_size=2).audit()
    assert sum(len(page.rows) for page in first.pages if page.domain == "runtime" and page.table == "task_runs") == 7
    assert all(set(row) == {"row_hash"} for page in first.pages for row in page.rows)
    assert "blob-hold" in first.candidates
    assert "blob-released" in first.candidates
    with sqlite3.connect(content) as conn:
        conn.execute("INSERT INTO content_holds(hold_id,blob_id,active) VALUES('h','blob-hold','1')")
    second = ReferenceAudit(tmp_path, page_size=2).audit(previous=first)
    assert "blob-hold" not in second.candidates
    assert "blob-hold" not in second.candidate_intersection


def test_symlink_database_is_rejected_and_no_wal_write(tmp_path: Path):
    _fixture(tmp_path)
    target = tmp_path / "real-workspace"
    target.mkdir()
    link = tmp_path / "workspace-link"
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ReadOnlyAdapterError):
        ReferenceAudit(link).audit()
    assert not Path(str(link) + ".memoryguard-wal").exists()


def test_fully_initialized_v2_workspace_has_no_schema_blockers(tmp_path: Path):
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

    layout = WorkspaceV2Layout(tmp_path)
    layout.ensure_dirs()
    for domain, paths in layout.databases.items():
        for path in paths:
            try:
                initialize_database(path, domain if domain != "projection" else "projection", layout=layout)
            except Exception:
                pass
    for store_cls in (RuntimeStore, MemoryAtomStore, RuleV2Store, EvidenceStore, ContentStore, CodeGraphStore, AssetStore, SkillStore):
        store_cls(tmp_path)
    ProjectionStore(tmp_path)
    result = ReferenceAudit(tmp_path).audit()
    assert not {item.code for item in result.blockers} & {"unknown_authoritative_table", "unknown_authoritative_column", "partial_schema", "missing_or_unsupported_marker", "metadata_marker_drift", "future_schema"}


def test_non_core_column_drift_is_blocked(tmp_path: Path):
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
    layout = WorkspaceV2Layout(tmp_path); layout.ensure_dirs()
    for domain, paths in layout.databases.items():
        for path in paths:
            try: initialize_database(path, domain if domain != "projection" else "projection", layout=layout)
            except Exception: pass
    for cls in (RuntimeStore, MemoryAtomStore, RuleV2Store, EvidenceStore, ContentStore, CodeGraphStore, AssetStore, SkillStore): cls(tmp_path)
    ProjectionStore(tmp_path)
    targets = ((layout.rules_db, "rule_definitions"), (layout.runtime_db, "task_runs"), (layout.codegraph_db, "symbols"))
    for path, table in targets:
        with sqlite3.connect(path) as conn: conn.execute(f'ALTER TABLE "{table}" ADD COLUMN evil_col TEXT')
    result = ReferenceAudit(tmp_path).audit()
    assert sum(item.code == "unknown_authoritative_column" for item in result.blockers) >= 3
