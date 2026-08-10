from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from memoryguard.migration.projections import V1ProjectionMigrator
from memoryguard.migration.projections import ProjectionMigrationError
from memoryguard.projection_v2 import (
    ProjectionError,
    ProjectionReadScope,
    ProjectionSchemaError,
    ProjectionStore,
    ScenarioProjector,
)
from memoryguard.storage.layout import WorkspaceV2Layout


def _scope(workspace: Path, **changes: str) -> ProjectionReadScope:
    values = {
        "workspace_id": str(workspace.resolve()),
        "agent_instance_id": "agent-a",
        "project_ref": "project-a",
        "provider": "provider-a",
        "share_group_id": "group-a",
        "sensitivity": "normal",
        "policy_class": "private",
    }
    values.update(changes)
    return ProjectionReadScope(**values)


def test_projection_is_reference_only_acl_exact_and_idempotent(tmp_path: Path) -> None:
    store = ProjectionStore(tmp_path)
    scope = _scope(tmp_path)
    projector = ScenarioProjector(store)
    first = projector.project(
        "daily",
        atoms=[{"atom_id": "atom-1", "atom_hash": "ah-1"}],
        evidence=[{"evidence_id": "evidence-1", "evidence_hash": "eh-1"}],
        scope=scope,
        metadata={"label": "derived", "body_hash": "abc123"},
    )
    again = projector.project(
        "daily",
        atoms=[{"atom_id": "atom-1", "atom_hash": "ah-1"}],
        evidence=[{"evidence_id": "evidence-1", "evidence_hash": "eh-1"}],
        scope=scope,
        metadata={"label": "derived", "body_hash": "abc123"},
    )
    assert again.projection_id == first.projection_id
    assert store.counts("scenario")["projections"] == 1
    assert store.get_projection("scenario", "daily", scope=scope) is not None

    # Every ACL column is exact; a different provider/group/sensitivity is a
    # neutral miss, not an existence leak.
    assert store.get_projection(
        "scenario", "daily", scope=_scope(tmp_path, provider="other")
    ) is None
    assert store.get_projection(
        "scenario", "daily", scope=_scope(tmp_path, share_group_id="other")
    ) is None
    assert store.get_projection(
        "scenario", "daily", scope=_scope(tmp_path, sensitivity="high")
    ) is None
    assert store.get_projection(
        "scenario", "daily", scope=_scope(tmp_path, policy_class="shared")
    ) is None

    changed = projector.project(
        "daily",
        atoms=[{"atom_id": "atom-1", "atom_hash": "ah-2"}],
        evidence=[{"evidence_id": "evidence-1", "evidence_hash": "eh-1"}],
        scope=scope,
        metadata={"label": "derived", "body_hash": "abc123"},
    )
    assert changed.projection_id != first.projection_id
    assert changed.generation == first.generation + 1
    assert store.counts("scenario")["projections"] == 2
    with store.connection("scenario") as conn:
        rows = conn.execute(
            "SELECT generation,payload_json FROM scenario_projections WHERE scenario_key=? ORDER BY generation",
            ("daily",),
        ).fetchall()
    assert [int(row[0]) for row in rows] == [0, 1]
    assert "abc123" in " ".join(str(row[1]) for row in rows)
    assert "must-not-copy" not in " ".join(str(row[1]) for row in rows)
    assert store.orphan_count("scenario") == 0
    assert store.integrity_check("scenario") == ["ok"]
    assert store.foreign_key_check("scenario") == []
    # Projection initialization creates directories only; it does not become
    # an authority by creating memory/evidence databases.
    assert not store.layout.memory_db.exists()
    assert not store.layout.evidence_db.exists()


def test_projection_requires_evidence_and_rejects_source_fields(tmp_path: Path) -> None:
    store = ProjectionStore(tmp_path)
    projector = ScenarioProjector(store)
    scope = _scope(tmp_path)
    with pytest.raises(ProjectionError):
        projector.project("missing-evidence", atoms=["atom-1"], evidence=[], scope=scope)
    with pytest.raises(ProjectionError):
        projector.project(
            "source-field",
            atoms=["atom-1"],
            evidence=[{"evidence_id": "evidence-1", "evidence_hash": "eh-1"}],
            scope=scope,
            metadata={"nested": {"conversation": "secret transcript"}},
        )
    with pytest.raises(ProjectionError):
        projector.project(
            "unknown-acl",
            atoms=["atom-1"],
            evidence=[{"evidence_id": "evidence-1", "evidence_hash": "eh-1"}],
            scope=_scope(tmp_path, provider="__UNKNOWN__"),
        )
    foreign_scope = ProjectionReadScope(workspace_id=str((tmp_path / "foreign").resolve()))
    with pytest.raises(ProjectionError):
        projector.project(
            "foreign-workspace",
            atoms=["atom-1"],
            evidence=[{"evidence_id": "evidence-1", "evidence_hash": "eh-1"}],
            scope=foreign_scope,
        )


def test_item_refs_require_matching_nonempty_evidence_hash(tmp_path: Path) -> None:
    store = ProjectionStore(tmp_path)
    scope = _scope(tmp_path)
    with pytest.raises(ProjectionError):
        store.put_projection(
            "scenario",
            "bad-link",
            source_digest="source",
            payload={"label": "derived"},
            scope=scope,
            evidence_links=[{"evidence_id": "e-1", "evidence_hash": "eh-1"}],
            item_refs=[{"atom_id": "a-1", "evidence_id": "e-missing", "evidence_hash": "eh-1"}],
        )
    with pytest.raises(ProjectionError):
        store.put_projection(
            "scenario",
            "bad-hash",
            source_digest="source",
            payload={"label": "derived"},
            scope=scope,
            evidence_links=[{"evidence_id": "e-1", "evidence_hash": "eh-1"}],
            item_refs=[{"atom_id": "a-1", "evidence_id": "e-1", "evidence_hash": ""}],
        )
    with pytest.raises(ProjectionError):
        store.put_projection(
            "scenario",
            "empty-link-hash",
            source_digest="source",
            payload={"label": "derived"},
            scope=scope,
            evidence_links=[{"evidence_id": "e-1", "evidence_hash": ""}],
        )
    assert store.counts("scenario")["projections"] == 0


def test_duplicate_evidence_relation_and_control_metadata_fail_closed(tmp_path: Path) -> None:
    store = ProjectionStore(tmp_path)
    scope = _scope(tmp_path)
    with pytest.raises(ProjectionError, match="duplicate evidence_id\\+relation"):
        store.put_projection(
            "scenario",
            "duplicate-link",
            source_digest="source",
            payload={"label": "derived"},
            scope=scope,
            evidence_links=[
                {"evidence_id": "e-1", "evidence_hash": "eh-1", "relation": "supports"},
                {"evidence_id": "e-1", "evidence_hash": "eh-1", "relation": "supports"},
            ],
        )
    projector = ScenarioProjector(store)
    with pytest.raises(ProjectionError):
        projector.project(
            "control-field",
            atoms=["atom-1"],
            evidence=[{"evidence_id": "e-1", "evidence_hash": "eh-1"}],
            scope=scope,
            metadata={"nested": {"safe": "ok", "role": "administrator"}},
        )
    assert store.counts("scenario")["projections"] == 0


def test_layout_object_requires_raw_workspace_and_rejects_symlink(tmp_path: Path) -> None:
    valid_layout = WorkspaceV2Layout(tmp_path)
    valid_store = ProjectionStore(valid_layout, source_workspace=tmp_path)
    assert valid_store.layout.workspace == tmp_path.resolve()
    with pytest.raises(ProjectionError):
        ProjectionStore(valid_layout)

    target = tmp_path / "layout-target"
    target.mkdir()
    link = tmp_path / "layout-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    symlink_layout = WorkspaceV2Layout(link)
    with pytest.raises(ProjectionError):
        ProjectionStore(symlink_layout)
    with pytest.raises(ProjectionError):
        ProjectionStore(symlink_layout, source_workspace=link)


def test_cross_key_head_tamper_is_neutral_and_rollback_rejects_wrong_key(tmp_path: Path) -> None:
    store = ProjectionStore(tmp_path)
    projector = ScenarioProjector(store)
    scope = _scope(tmp_path)
    first = projector.project(
        "key-a", atoms=["atom-a"], evidence=[{"evidence_id": "e-a", "evidence_hash": "eh-a"}], scope=scope
    )
    second = projector.project(
        "key-b", atoms=["atom-b"], evidence=[{"evidence_id": "e-b", "evidence_hash": "eh-b"}], scope=scope
    )
    with pytest.raises(ProjectionError):
        store.rollback("scenario", "key-a", second.projection_id)
    with store.connection("scenario") as conn:
        original_head = conn.execute(
            "SELECT current_projection_id,generation FROM projection_heads WHERE projection_key=?", ("key-a",)
        ).fetchone()
    assert tuple(original_head) == (first.projection_id, first.generation)

    # Simulate a storage attack that points key-a at key-b.  Reads and
    # destructive head operations must stay neutral and leave the tampered
    # row untouched rather than mutating key-b's history.
    with sqlite3.connect(store.layout.scenario_db) as conn:
        conn.execute(
            "UPDATE projection_heads SET current_projection_id=? WHERE projection_kind='scenario' AND projection_key='key-a'",
            (second.projection_id,),
        )
        conn.commit()
    assert store.get_projection("scenario", "key-a", scope=scope) is None
    with pytest.raises(ProjectionError):
        store.tombstone("scenario", "key-a", reason="attack")
    with pytest.raises(ProjectionError):
        projector.project(
            "key-a",
            atoms=["atom-a-new"],
            evidence=[{"evidence_id": "e-a-new", "evidence_hash": "eh-a-new"}],
            scope=scope,
        )
    with store.connection("scenario") as conn:
        tampered_head = conn.execute(
            "SELECT current_projection_id,generation FROM projection_heads WHERE projection_key=?", ("key-a",)
        ).fetchone()
    assert tuple(tampered_head) == (second.projection_id, second.generation)


def test_generation_uses_head_and_rows_and_tombstone_is_identical_only_idempotent(tmp_path: Path) -> None:
    store = ProjectionStore(tmp_path)
    projector = ScenarioProjector(store)
    scope = _scope(tmp_path)
    first = projector.project(
        "monotonic", atoms=["a-1"], evidence=[{"evidence_id": "e-1", "evidence_hash": "eh-1"}], scope=scope
    )
    second = projector.project(
        "monotonic", atoms=["a-2"], evidence=[{"evidence_id": "e-2", "evidence_hash": "eh-2"}], scope=scope
    )
    # Lowering the head generation cannot cause reuse of an immutable row's
    # generation; the rows' max is part of the next-generation calculation.
    with sqlite3.connect(store.layout.scenario_db) as conn:
        conn.execute(
            "UPDATE projection_heads SET generation=0 WHERE projection_kind='scenario' AND projection_key='monotonic'"
        )
        conn.commit()
    third = projector.project(
        "monotonic", atoms=["a-3"], evidence=[{"evidence_id": "e-3", "evidence_hash": "eh-3"}], scope=scope
    )
    assert (first.generation, second.generation, third.generation) == (0, 1, 2)

    tombstone_1 = store.tombstone("scenario", "monotonic", reason="same-reason")
    before = store.counts("scenario")
    with store.connection("scenario") as conn:
        events_before = int(conn.execute("SELECT COUNT(*) FROM projection_head_events").fetchone()[0])
    tombstone_2 = store.tombstone("scenario", "monotonic", reason="same-reason")
    assert tombstone_2 == tombstone_1
    assert store.counts("scenario") == before
    with store.connection("scenario") as conn:
        assert int(conn.execute("SELECT COUNT(*) FROM projection_head_events").fetchone()[0]) == events_before
    tombstone_3 = store.tombstone("scenario", "monotonic", reason="different-reason")
    assert tombstone_3 != tombstone_1
    assert store.counts("scenario")["tombstones"] == before["tombstones"] + 1


def test_rollback_rejects_acl_domain_change(tmp_path: Path) -> None:
    store = ProjectionStore(tmp_path)
    projector = ScenarioProjector(store)
    old_scope = _scope(tmp_path, provider="provider-old")
    new_scope = _scope(tmp_path, provider="provider-new")
    old = projector.project(
        "acl-history", atoms=["old"], evidence=[{"evidence_id": "old-e", "evidence_hash": "old-h"}], scope=old_scope
    )
    current = projector.project(
        "acl-history", atoms=["new"], evidence=[{"evidence_id": "new-e", "evidence_hash": "new-h"}], scope=new_scope
    )
    with pytest.raises(ProjectionError):
        store.rollback("scenario", "acl-history", old.projection_id)
    with store.connection("scenario") as conn:
        head = conn.execute(
            "SELECT current_projection_id FROM projection_heads WHERE projection_key=?", ("acl-history",)
        ).fetchone()
    assert str(head[0]) == current.projection_id


def test_migrator_rejects_symlinked_or_external_legacy_sources(tmp_path: Path) -> None:
    real_workspace = tmp_path / "real-workspace"
    real_workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escape.json").write_text("{}", encoding="utf-8")

    workspace_link = tmp_path / "workspace-link"
    source_link = tmp_path / "source-link"
    file_link_root = real_workspace / ".memoryguard" / "projections"
    file_link_root.mkdir(parents=True)
    file_link = file_link_root / "escape.json"
    try:
        workspace_link.symlink_to(real_workspace, target_is_directory=True)
        source_link.symlink_to(outside, target_is_directory=True)
        file_link.symlink_to(outside / "escape.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ProjectionMigrationError):
        V1ProjectionMigrator(workspace_link)
    with pytest.raises(ProjectionMigrationError):
        V1ProjectionMigrator(real_workspace, source_root=source_link)
    with pytest.raises(ProjectionMigrationError):
        V1ProjectionMigrator(real_workspace).migrate()

    # A regular explicit root outside the workspace is an authorized root;
    # containment is lexical and does not follow links.
    authorized = outside / "authorized"
    authorized.mkdir()
    (authorized / "ok.json").write_text("{}", encoding="utf-8")
    report = V1ProjectionMigrator(real_workspace, source_root=authorized).migrate()
    assert report.ok and report.files == 1


def test_projection_transaction_failure_leaves_no_half_row(tmp_path: Path) -> None:
    store = ProjectionStore(tmp_path)
    projector = ScenarioProjector(store)
    scope = _scope(tmp_path)
    before = store.counts("scenario")
    with pytest.raises(ProjectionError):
        projector.project(
            "will-fail",
            atoms=["atom-f"],
            evidence=[{"evidence_id": "evidence-f", "evidence_hash": "eh-f"}],
            scope=scope,
            fail_at="after_links",
        )
    assert store.counts("scenario") == before
    assert store.orphan_count("scenario") == 0
    assert store.integrity_check("scenario") == ["ok"]
    assert store.foreign_key_check("scenario") == []


def test_future_projection_marker_fails_closed_without_mutation(tmp_path: Path) -> None:
    store = ProjectionStore(tmp_path)
    db_path = store.layout.scenario_db
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE projection_schema_meta SET value='999' WHERE key='version'"
        )
        conn.commit()
    before = db_path.read_bytes()
    with pytest.raises(ProjectionSchemaError):
        ProjectionStore(tmp_path)
    assert db_path.read_bytes() == before


def test_projection_workspace_reparse_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "real-workspace"
    target.mkdir()
    link = tmp_path / "workspace-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ProjectionError):
        ProjectionStore(link)


def test_tombstone_and_rollback_keep_immutable_history(tmp_path: Path) -> None:
    store = ProjectionStore(tmp_path)
    projector = ScenarioProjector(store)
    scope = _scope(tmp_path)
    first = projector.project("history", atoms=["atom-1"], evidence=[{"evidence_id": "e-1", "evidence_hash": "eh-1"}], scope=scope)
    second = projector.project("history", atoms=["atom-2"], evidence=[{"evidence_id": "e-2", "evidence_hash": "eh-2"}], scope=scope)
    tombstone_id = store.tombstone("scenario", "history", reason="source-deleted")
    assert tombstone_id
    assert store.get_projection("scenario", "history", scope=scope) is None
    assert store.rollback("scenario", "history", second.projection_id)
    restored = store.get_projection("scenario", "history", scope=scope)
    assert restored is not None
    assert restored.projection_id == second.projection_id
    with store.connection("scenario") as conn:
        rows = conn.execute(
            "SELECT projection_id,generation FROM scenario_projections WHERE scenario_key=? ORDER BY generation",
            ("history",),
        ).fetchall()
        events = conn.execute(
            "SELECT event_type FROM projection_head_events WHERE projection_key=? ORDER BY rowid",
            ("history",),
        ).fetchall()
    assert {str(row[0]) for row in rows} == {first.projection_id, second.projection_id}
    assert [str(row[0]) for row in events] == ["publish", "publish", "tombstone", "rollback"]
    assert store.counts("scenario")["tombstones"] == 1


def test_legacy_projection_migration_is_read_only_reference_only_and_idempotent(
    tmp_path: Path,
) -> None:
    scenario_root = tmp_path / ".memoryguard" / "projections"
    profile_root = tmp_path / ".memoryguard" / "agent-profiles"
    scenario_root.mkdir(parents=True)
    profile_root.mkdir(parents=True)
    scenario = scenario_root / "daily.json"
    profile = profile_root / "agent-a.json"
    scenario.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "memory_id": "atom-1",
                        "canonical_hash": "ah-1",
                        "body": "must-not-copy",
                        "evidence_links": [{"evidence_id": "e-1", "evidence_hash": "eh-1"}],
                    }
                ],
                "metadata": {
                    "label": "derived",
                    "raw_content": "must-not-copy",
                    "role": "administrator",
                    "nested": {"permission": "grant-all"},
                },
                "unknown_field": "ledger-me",
            }
        ),
        encoding="utf-8",
    )
    profile.write_text(
        json.dumps(
            {
                "profile_id": "profile-a",
                "meta": {"display": "Agent A"},
                "body": "must-not-copy",
            }
        ),
        encoding="utf-8",
    )
    source_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (scenario, profile)}
    migrator = V1ProjectionMigrator(tmp_path)
    report = migrator.migrate()
    assert report.ok
    assert report.files == 2
    assert report.projections == 2
    assert report.ledger >= 2
    assert {hashlib.sha256(path.read_bytes()).hexdigest() for path in (scenario, profile)} == set(source_hashes.values())

    store = ProjectionStore(tmp_path)
    scenario_scope = ProjectionReadScope(
        workspace_id=str(tmp_path.resolve()), provider="legacy", sensitivity="normal", policy_class="private"
    )
    record = store.get_projection("scenario", "daily", scope=scenario_scope)
    assert record is not None
    encoded = json.dumps(record.payload, ensure_ascii=False)
    assert "must-not-copy" not in encoded
    assert "administrator" not in encoded
    assert "grant-all" not in encoded
    assert record.evidence_links[0]["evidence_id"] == "e-1"
    assert store.get_projection("scenario", "daily", scope=_scope(tmp_path)) is None
    assert store.counts("scenario")["ledger"] >= 2
    assert store.counts("profile")["projections"] == 1

    before_counts = (store.counts("scenario"), store.counts("profile"))
    rerun = migrator.migrate()
    assert rerun.ok
    assert (store.counts("scenario"), store.counts("profile")) == before_counts
    assert {hashlib.sha256(path.read_bytes()).hexdigest() for path in (scenario, profile)} == set(source_hashes.values())


def test_legacy_unreadable_source_fails_without_writing_target(tmp_path: Path) -> None:
    source_root = tmp_path / ".memoryguard" / "projections"
    source_root.mkdir(parents=True)
    (source_root / "broken.json").write_bytes(b"{not-json")
    # Constructor is intentionally allowed to initialize empty projection
    # targets; the failed source row itself must not be partially published.
    migrator = V1ProjectionMigrator(tmp_path)
    before = migrator.store.counts("scenario")
    with pytest.raises(Exception):
        migrator.migrate()
    assert migrator.store.counts("scenario") == before
    assert migrator.store.integrity_check("scenario") == ["ok"]
    assert migrator.store.foreign_key_check("scenario") == []
