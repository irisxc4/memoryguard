from __future__ import annotations

import json
from pathlib import Path

import pytest

import memoryguard.runtime_v2.safe_services as safe_services
from memoryguard.runtime_v2.safe_services import (
    ImportPreviewService,
    PureSourceReadService,
    RuntimeDiagnosticsService,
)


def _write_config(workspace: Path, roots: list[dict]) -> None:
    config_dir = workspace / ".memoryguard"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"sources": roots}), encoding="utf-8"
    )


def _write_local_config(workspace: Path, roots: list[dict]) -> None:
    config_dir = workspace / ".memoryguard"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.local.json").write_text(
        json.dumps({"sources": roots}), encoding="utf-8"
    )


def _root(root_id: str, path: Path, *, root_type: str = "selected_directory") -> dict:
    return {
        "root_id": root_id,
        "type": root_type,
        "display_name": root_id,
        "path": str(path),
        "scope": "project",
        "recursive": True,
        "follow_symlinks": False,
        "include": ["**/*.md"],
        "exclude": [],
        "enabled": True,
    }


def test_missing_source_is_no_source_and_does_not_create_layout(tmp_path: Path) -> None:
    service = PureSourceReadService(tmp_path)
    result = service.list_sources({}, context={"agent_instance_id": "agent-a"})

    assert result["status"] == "NO_SOURCE"
    assert result["sources"] == []
    assert not (tmp_path / ".memoryguard").exists()


def test_source_read_and_preview_are_contained_and_hash_only(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    note = source / "note.md"
    note.write_text("secret body", encoding="utf-8")
    _write_config(tmp_path, [_root("src-one", source)])

    read = PureSourceReadService(tmp_path)
    listed = read.list_sources({}, context={"agent_instance_id": "agent-a"})
    assert listed["status"] == "READY"
    assert listed["sources"][0]["root_id"] == "src-one"
    assert str(source) not in json.dumps(listed)

    preview = ImportPreviewService(tmp_path).preview(
        {"path": str(note), "agent_instance_id": "spoofed"},
        context={"agent_instance_id": "agent-a"},
    )
    encoded = json.dumps(preview)
    assert preview["status"] == "READY"
    assert "secret body" not in encoded
    assert "sha256" in encoded
    assert str(note) not in encoded


def test_import_preview_rejects_out_of_scope_without_writes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_config(tmp_path, [_root("src-one", source)])
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")

    result = ImportPreviewService(tmp_path).preview(
        {"path": str(outside)}, context={"agent_instance_id": "agent-a"}
    )
    assert result["status"] == "BLOCKED"
    assert result["code"] == "path_out_of_scope"
    assert not (tmp_path / ".memoryguard" / "staging").exists()


def test_runtime_diagnostics_redacts_payload_workspace_and_secrets(tmp_path: Path) -> None:
    service = RuntimeDiagnosticsService(
        tmp_path,
        version_provider=lambda: "9.9.9",
        status_provider=lambda: {"state": "V2_ACTIVE", "command": "python secret"},
    )
    result = service.memoryguard_runtime_processes(
        {"workspace": "C:/attacker", "secret": "do-not-show"},
        context={"is_admin": False},
    )
    encoded = json.dumps(result)
    assert result["status"] == "READY"
    assert result["memoryguard_version"] == "9.9.9"
    assert "attacker" not in encoded
    assert "do-not-show" not in encoded
    assert "command" not in encoded


def test_provider_internal_typeerror_is_not_retried_without_workspace(tmp_path: Path) -> None:
    calls: list[Path] = []

    def provider(workspace: Path) -> str:
        calls.append(workspace)
        raise TypeError("provider implementation failure")

    service = RuntimeDiagnosticsService(tmp_path, version_provider=provider)
    result = service.memoryguard_runtime_processes({}, context={})

    assert result["status"] == "READY"
    assert result["memoryguard_version"] == "unknown"
    assert calls == [tmp_path.resolve()]


def test_local_config_overrides_shared_root_id_without_duplicate(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    local = tmp_path / "local"
    shared.mkdir()
    local.mkdir()
    _write_config(tmp_path, [_root("same-root", shared)])
    _write_local_config(tmp_path, [_root("same-root", local)])

    result = PureSourceReadService(tmp_path).list_sources({}, context={})

    assert result["status"] == "READY"
    assert result["total"] == 1
    assert result["sources"][0]["reference"] == "workspace:local"


def test_effective_config_unknown_field_still_blocks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    invalid = _root("source", source)
    invalid["attacker_field"] = "spoof"
    _write_config(tmp_path, [invalid])

    result = PureSourceReadService(tmp_path).list_sources({}, context={})

    assert result == {
        "ok": False,
        "status": "BLOCKED",
        "service": "source_read",
        "code": "unknown_source_root_fields",
        "error": "unknown_source_root_fields",
    }


def test_single_file_inventory_and_preview_enforce_size_budget(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    note = source / "note.md"
    note.write_text("0123456789", encoding="utf-8")
    _write_config(tmp_path, [_root("src-one", source)])
    service = ImportPreviewService(tmp_path)

    inventory = service.inventory(
        {"path": str(note), "max_single_file": 2}, context={}
    )
    preview = service.preview(
        {"path": str(note), "max_single_file": 2}, context={}
    )

    assert inventory["status"] == "READY"
    assert inventory["candidate_count"] == 0
    assert inventory["truncated_count"] == 1
    assert preview["status"] == "READY"
    assert preview["summary"] == {
        "candidate_count": 0,
        "total_size": 0,
        "skipped_count": 1,
        "supported_count": 0,
    }


def test_preview_hash_timeout_is_blocked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    note = source / "note.md"
    note.write_text("body", encoding="utf-8")
    _write_config(tmp_path, [_root("src-one", source)])

    # The first two reads admit the candidate; the next check expires while
    # hashing, proving the hash loop has its own deadline guard.
    ticks = iter((0.0, 0.0, 0.0, 2.0))
    monkeypatch.setattr(safe_services.time, "monotonic", lambda: next(ticks))

    result = ImportPreviewService(tmp_path).preview(
        {"path": str(note), "timeout_seconds": 1}, context={}
    )

    assert result["ok"] is False
    assert result["status"] == "BLOCKED"
    assert result["code"] == "scan_timeout"
