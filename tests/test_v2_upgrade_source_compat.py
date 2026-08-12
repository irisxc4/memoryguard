from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

from memoryguard.migration.framework import V1Reader
from memoryguard.migration.v2_validator import V2MigrationValidator
from memoryguard.migration.workspace_prepare import prepare_v2_workspace
from memoryguard.storage.layout import WorkspaceV2Layout


def _add_legacy_receipt(history: Path) -> tuple[str, ...]:
    values = (
        "legacy-delete-1",
        "delete",
        hashlib.sha256(b"legacy-delete-payload").hexdigest(),
        '{"deleted_sessions":1,"invalidated_evidence_links":0,"long_term_memories_deleted":0}',
        "2026-01-01T00:00:00+00:00",
    )
    with sqlite3.connect(history) as conn:
        conn.execute(
            "CREATE TABLE history_mutation_receipts("
            "idempotency_key TEXT PRIMARY KEY, operation TEXT NOT NULL, "
            "payload_digest TEXT NOT NULL, result_json TEXT NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO history_mutation_receipts VALUES (?,?,?,?,?)", values)
        conn.commit()
    return values


def _fixture(root: Path) -> tuple[Path, tuple[str, ...], Path]:
    # Reuse the established 0.6.2-shaped Phase-2 fixture, then add the
    # authority table that the older fixture omitted.
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from test_v2_phase2_integration import _fixture as make_fixture

    _group, _group2, history, _hashes = make_fixture(root)
    receipt = _add_legacy_receipt(history)
    backup_manifest = (
        root
        / ".memoryguard"
        / "migration-backups"
        / "old-batch"
        / "source-snapshot"
        / "workspace"
        / ".memoryguard"
        / "native_releases"
        / "legacy-manifest.json"
    )
    backup_manifest.parent.mkdir(parents=True, exist_ok=True)
    backup_manifest.write_text(
        json.dumps({"schema_version": 1, "scope": "must-not-be-scanned"}),
        encoding="utf-8",
    )
    return history, receipt, backup_manifest


def _backup_paths(payload: dict) -> list[str]:
    return [
        str(item.get("path") or item.get("source") or item.get("backup") or "")
        for item in (payload.get("files") or [])
        if isinstance(item, dict)
    ]


def test_062_receipts_and_nested_backup_manifest_survive_dry_run_apply(tmp_path: Path) -> None:
    history, receipt, backup_manifest = _fixture(tmp_path)

    snapshot = V1Reader(tmp_path, data_home=tmp_path / "data").scan(strict=False)
    assert str(backup_manifest.resolve()) not in {item.path for item in snapshot.items}
    assert not any("migration-backups" in item.path for item in snapshot.items)

    dry_run_before = prepare_v2_workspace(tmp_path, data_home=tmp_path / "data", apply=False)
    assert dry_run_before["status"] == "DRY_RUN"
    assert not any("migration-backups" in path for path in _backup_paths(dry_run_before))

    applied = prepare_v2_workspace(tmp_path, data_home=tmp_path / "data", apply=True)
    assert applied["status"] == "V2_BUILDING", applied
    assert applied["ok"] is True, applied
    assert applied["validator"]["status"] == "PASS", applied["validator"]
    content_metrics = applied["validator"]["domains"]["content"]["metrics"]
    receipts = content_metrics["history_mutation_receipts"]
    assert receipts["source_count"] == receipts["target_count"] == 1
    assert receipts["source_digest"] == receipts["target_digest"]
    assert receipts["loss"] == 0
    assert content_metrics["history_mutation_receipts_loss"] == 0

    layout = WorkspaceV2Layout(tmp_path)
    with sqlite3.connect(layout.content_db) as conn:
        assert tuple(
            conn.execute(
                "SELECT idempotency_key,operation,payload_digest,result_json,created_at "
                "FROM history_mutation_receipts"
            ).fetchone()
        ) == receipt

    dry_run_after = prepare_v2_workspace(tmp_path, data_home=tmp_path / "data", apply=False)
    assert dry_run_after["status"] == "DRY_RUN"
    assert dry_run_after["validator"]["status"] == "PASS"
    assert not any("migration-backups" in path for path in _backup_paths(dry_run_after))
    assert V2MigrationValidator(tmp_path, data_home=tmp_path / "data").validate().status == "PASS"
    assert history.is_file() and backup_manifest.is_file()
