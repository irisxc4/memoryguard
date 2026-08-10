from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from memoryguard.assets_v2 import (
    UNKNOWN_ACL,
    AssetConflictError,
    AssetMutationContext,
    AssetPathError,
    AssetReadScope,
    AssetSchemaError,
    AssetStore,
)
from memoryguard.migration.assets import V1AssetMigrator
from memoryguard.storage.layout import WorkspaceV2Layout


def _ctx(root: Path, *, agent: str = "agent-a", project: str = "project-a", provider: str = "codex", group: str = "group-a", runtime: str = "runtime-a", namespace: str = "ns-a", admin: bool = False, authority: str = "manual") -> AssetMutationContext:
    return AssetMutationContext(
        namespace_id=namespace,
        workspace_id=str(root),
        agent_instance_id=agent,
        project_ref=project,
        provider=provider,
        share_group_id=group,
        runtime_role=runtime,
        actor=agent,
        authority=authority,
        admin=admin,
    )


def test_asset_registry_metadata_only_acl_and_idempotency(tmp_path: Path) -> None:
    store = AssetStore(tmp_path)
    ctx = _ctx(tmp_path)
    with pytest.raises(PermissionError):
        store.register_asset("raw")
    asset = store.register_asset("manifest", asset_kind="source_manifest", metadata={"media_type": "application/json"}, context=ctx)
    replay = store.register_asset("manifest", asset_kind="source_manifest", metadata={"media_type": "application/json"}, context=ctx)
    assert replay.asset_id == asset.asset_id
    with pytest.raises(AssetConflictError):
        store.register_asset("manifest", asset_kind="source_manifest", metadata={"media_type": "text/plain"}, context=ctx)
    version = store.register_version(asset.asset_id, "v1", content_hash="a" * 64, size_bytes=2, context=ctx)
    assert store.register_version(asset.asset_id, "v1", content_hash="a" * 64, size_bytes=2, context=ctx).version_id == version.version_id
    with pytest.raises(ValueError):
        store.register_version(asset.asset_id, "v2", content_hash="b", metadata={"body": "must not persist"}, context=ctx)
    assert store.get_asset(asset.asset_id, scope=AssetReadScope(**ctx.to_dict())) is not None
    assert store.get_asset(asset.asset_id, scope=_ctx(tmp_path, agent="other")) is None
    assert store.integrity_check() == ["ok"]


def test_asset_locations_reject_traversal_symlink_and_keep_hash_only(tmp_path: Path) -> None:
    store = AssetStore(tmp_path)
    ctx = _ctx(tmp_path)
    asset = store.register_asset("x", context=ctx)
    with pytest.raises(AssetPathError):
        store.register_location(asset.asset_id, "../outside", context=ctx)
    outside = tmp_path / "outside"
    outside.write_bytes(b"binary payload")
    location = store.register_location(asset.asset_id, "outside", path=outside, context=ctx)
    assert location.content_hash and location.size_bytes == len(b"binary payload")
    with store.connection() as conn:
        row = conn.execute("SELECT * FROM asset_locations WHERE location_id=?", (location.location_id,)).fetchone()
        assert "binary payload" not in json.dumps(dict(row))
    if hasattr(Path, "symlink_to"):
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            pass
        else:
            with pytest.raises(AssetPathError):
                store.register_location(asset.asset_id, "x", root=link, context=ctx)


def test_unknown_acl_is_existence_neutral(tmp_path: Path) -> None:
    store = AssetStore(tmp_path)
    ctx = _ctx(tmp_path, provider=UNKNOWN_ACL, authority="migration", admin=True)
    asset = store.register_asset("unknown", provider=UNKNOWN_ACL, context=ctx)
    assert store.get_asset(asset.asset_id, scope=AssetReadScope(namespace_id="ns-a", workspace_id=str(tmp_path), agent_instance_id="agent-a", project_ref="project-a", provider="codex", share_group_id="group-a", runtime_role="runtime-a")) is None
    assert store.list_unknown_ledger()


def test_tombstone_hold_gc_and_release(tmp_path: Path) -> None:
    store = AssetStore(tmp_path)
    ctx = _ctx(tmp_path, admin=True, authority="system")
    asset = store.register_asset("deletable", context=ctx)
    tomb = store.tombstone(asset.asset_id, reason="remove", context=ctx)
    assert tomb.active and store.get_asset(asset.asset_id, scope=ctx) is None
    assert store.gc(context=ctx)["deleted_assets"] == []
    holds = store.list_holds(asset_id=asset.asset_id, scope=ctx)
    assert holds
    store.release_hold(holds[0].hold_id, context=ctx)
    assert asset.asset_id in store.gc(context=ctx)["deleted_assets"]
    assert store.integrity_check() == ["ok"]


def test_future_marker_fails_closed_and_readonly_does_not_create(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        AssetStore(tmp_path, readonly=True)
    store = AssetStore(tmp_path)
    db = store.db_path
    before = db.read_bytes()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE asset_schema_meta SET value='999'")
        conn.commit()
    changed = db.read_bytes()
    with pytest.raises(AssetSchemaError):
        AssetStore(tmp_path)
    assert db.read_bytes() == changed


def test_metadata_exact_keys_allow_structural_facts_and_reject_nested_control_zero_write(tmp_path: Path) -> None:
    store = AssetStore(tmp_path)
    ctx = _ctx(tmp_path)
    allowed = {
        "provider": "codex",
        "provider_id": "provider-1",
        "content_hash": "a" * 64,
        "content_digest": "b" * 64,
        "output_hash": "c" * 64,
        # Substring matches are not sensitive fields.
        "provider_name": "safe",
        "token_count": 3,
    }
    asset = store.register_asset("structural", metadata=allowed, context=ctx)
    assert asset.metadata["provider"] == "codex"
    before = store.counts()
    with pytest.raises(ValueError):
        store.register_asset("rejected", metadata={"nested": {"api_key": "secret"}}, context=ctx)
    assert store.counts() == before


def test_layout_object_requires_raw_source_and_readonly_outbox_is_zero_write(tmp_path: Path) -> None:
    layout = WorkspaceV2Layout(tmp_path)
    with pytest.raises(AssetPathError):
        AssetStore(layout)
    store = AssetStore(tmp_path)
    ctx = _ctx(tmp_path, admin=True, authority="system")
    store.register_asset("outbox", context=ctx)
    event = store.pending_outbox()[0]
    before = store.db_path.read_bytes()
    readonly = AssetStore(tmp_path, readonly=True)
    with pytest.raises(PermissionError):
        readonly.mark_outbox(event.event_id, context=ctx)
    assert store.db_path.read_bytes() == before


def test_gc_blocks_reference_targets_and_records_idempotent_evidence(tmp_path: Path) -> None:
    store = AssetStore(tmp_path)
    ctx = _ctx(tmp_path, admin=True, authority="system")
    victim = store.register_asset("victim", context=ctx)
    keeper = store.register_asset("keeper", context=ctx)
    version = store.register_version(victim.asset_id, "v1", content_hash="a" * 64, context=ctx)
    location = store.register_location(victim.asset_id, "victim.bin", version_id=version.version_id, context=ctx)
    store.register_reference(keeper.asset_id, "points_to_location", location.location_id, context=ctx)
    store.tombstone(victim.asset_id, context=ctx)
    for hold in store.list_holds(asset_id=victim.asset_id, scope=ctx):
        store.release_hold(hold.hold_id, context=ctx)
    blocked = store.gc(context=ctx)
    assert blocked["deleted_assets"] == []
    assert victim.asset_id in blocked["blocked_assets"]
    assert store.counts()["asset_audit"] == store.counts()["asset_outbox"]


def test_v1_asset_migration_reads_known_files_without_source_mutation_or_body(tmp_path: Path) -> None:
    source = tmp_path / ".memoryguard" / "agent-profiles" / "codex.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"agent_instance_id": "a", "share_group_id": "g", "provider": "codex", "body": "secret"}), encoding="utf-8")
    before = source.read_bytes()
    report = V1AssetMigrator(tmp_path).migrate()
    assert report.ok and report.migrated == 1
    assert source.read_bytes() == before
    store = AssetStore(tmp_path)
    with store.connection() as conn:
        metadata = conn.execute("SELECT metadata_json FROM assets").fetchone()[0]
        assert "secret" not in metadata
        assert conn.execute("SELECT COUNT(*) FROM asset_migration_map").fetchone()[0] == 1


def test_asset_migration_fault_rolls_back(tmp_path: Path) -> None:
    first = tmp_path / ".memoryguard" / "source-manifest.json"
    second = tmp_path / ".memoryguard" / "native_releases" / "r1" / "manifest.json"
    second.parent.mkdir(parents=True)
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")
    with pytest.raises(Exception):
        V1AssetMigrator(tmp_path).migrate(fail_after=1)
    store = AssetStore(tmp_path)
    assert store.counts()["assets"] == 0
