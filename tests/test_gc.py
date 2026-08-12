import gzip
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.gc import GcPlan, GcPlanItem, MemoryGuardGc
from memoryguard.gui import GovernanceApi
from memoryguard.access_context import AccessContext
from memoryguard.desktop_executor import SERVER_ADMIN_AGENT_ID
from memoryguard.migration.upgrade import run_upgrade
from memoryguard.schema_v3 import _now_iso


def _mg(workspace: Path) -> Path:
    d = workspace / ".memoryguard"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _old_iso(days: int = 40) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _touch_old(path: Path, days: int = 40) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    old = time.time() - (days * 86400)
    os.utime(path, (old, old))


def _make_native_release(mg: Path, release_id: str, *, status: str, created_at: str) -> Path:
    release_dir = mg / "native_releases" / release_id
    backup = release_dir / "backup"
    staged = release_dir / "staged"
    backup.mkdir(parents=True)
    staged.mkdir(parents=True)
    (backup / "file.bak").write_text("backup content", encoding="utf-8")
    (staged / "file.md").write_text("staged content", encoding="utf-8")
    manifest = {
        "release_id": release_id,
        "status": status,
        "created_at": created_at,
        "files": [],
    }
    (release_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return release_dir


def _active_gui_api(root: Path) -> GovernanceApi:
    """Use V2 maintenance/task envelopes after explicit activation."""
    ready = run_upgrade(root, data_home=root, apply=True)
    assert ready["status"] == "V2_READY", ready
    active = run_upgrade(
        root,
        data_home=root,
        apply=True,
        confirm="V2_ACTIVE",
    )
    assert active["v2_active"] is True, active
    return GovernanceApi(
        str(root),
        _trusted_access_context=AccessContext(
            trusted_agent_id=SERVER_ADMIN_AGENT_ID,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="gc-test-session",
            session_source="transport",
            session_trusted=True,
        ),
    )


def test_gc_strips_expired_native_release_backup_and_staged(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mg = _mg(workspace)
    release_dir = _make_native_release(
        mg,
        "nrel-old",
        status="applied_verified",
        created_at=_old_iso(40),
    )

    gc = MemoryGuardGc(workspace, older_than_days=30)
    plan = gc.plan(dry_run=False)
    assert any(item.path.endswith("backup") or item.path.endswith("staged") for item in plan.items)

    result = gc.apply(plan, confirmed=True)
    assert result["ok"] is True
    assert (release_dir / "manifest.json").exists()
    assert not (release_dir / "backup").exists()
    assert not (release_dir / "staged").exists()


def test_gc_keeps_recent_native_release_artifacts(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mg = _mg(workspace)
    release_dir = _make_native_release(
        mg,
        "nrel-new",
        status="applied_verified",
        created_at=_now_iso(),
    )

    gc = MemoryGuardGc(workspace, older_than_days=30)
    plan = gc.plan(dry_run=False)
    assert not plan.items
    assert (release_dir / "backup").exists()
    assert (release_dir / "staged").exists()


def test_gc_keeps_only_recent_snapshots(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mg = _mg(workspace)
    snapshots = mg / "snapshots"
    snapshots.mkdir()
    for idx in range(5):
        snap_dir = snapshots / f"snap-{idx}"
        snap_dir.mkdir()
        (snap_dir / "sources.json").write_text("{}", encoding="utf-8")
        _touch_old(snap_dir / "sources.json", days=idx)

    gc = MemoryGuardGc(workspace, keep_snapshots=3)
    plan = gc.plan(dry_run=False)
    delete_paths = {item.path for item in plan.items if item.action == "delete_dir"}
    assert len(delete_paths) == 2

    result = gc.apply(plan, confirmed=True)
    assert result["ok"] is True
    remaining = sorted(p.name for p in snapshots.iterdir() if p.is_dir())
    assert len(remaining) == 3


def test_gc_dry_run_does_not_delete(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mg = _mg(workspace)
    release_dir = _make_native_release(
        mg,
        "nrel-dry",
        status="rolled_back",
        created_at=_old_iso(40),
    )

    gc = MemoryGuardGc(workspace, older_than_days=30)
    plan = gc.plan(dry_run=True)
    assert plan.dry_run is True
    assert plan.items

    assert (release_dir / "backup").exists()
    assert (release_dir / "staged").exists()


def test_gc_skips_archived_agents(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mg = _mg(workspace)
    archived = mg / "cleanup" / "archived-agents" / "agent-1"
    archived.mkdir(parents=True)
    secret_file = archived / "MEMORY.md"
    secret_file.write_text("do not delete", encoding="utf-8")

    gc = MemoryGuardGc(workspace)
    plan = GcPlan(
        items=[
            GcPlanItem(
                path=str(secret_file),
                action="delete_file",
                reason="test",
                bytes_estimate=1,
                reversible=False,
            )
        ],
        total_bytes=1,
        dry_run=False,
    )
    result = gc.apply(plan, confirmed=True)
    assert result["ok"] is False
    assert secret_file.exists()


def test_gc_rotates_large_decisions_jsonl(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mg = _mg(workspace)
    ir_dir = mg / "ir"
    ir_dir.mkdir()
    decisions = ir_dir / "decisions.jsonl"
    decisions.write_text("line\n" * 200, encoding="utf-8")

    gc = MemoryGuardGc(workspace, decisions_rotate_bytes=50)
    plan = gc.plan(dry_run=False)
    assert len(plan.items) == 1
    assert plan.items[0].action == "rotate_jsonl"

    result = gc.apply(plan, confirmed=True)
    assert result["ok"] is True
    assert decisions.exists()
    assert decisions.read_text(encoding="utf-8") == ""
    archives = list(ir_dir.glob("decisions.*.jsonl.gz"))
    assert len(archives) == 1
    with gzip.open(archives[0], "rt", encoding="utf-8") as fh:
        rotated = fh.read()
    assert "line" in rotated


def test_gc_removes_old_plans_and_staging(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mg = _mg(workspace)
    plans_dir = mg / "plans"
    staging_root = mg / "staging"
    plans_dir.mkdir()
    staging_root.mkdir()
    plan_id = "plan-old"
    plan_file = plans_dir / f"{plan_id}.json"
    plan_file.write_text(
        json.dumps({"plan_id": plan_id, "created_at": _old_iso(10)}),
        encoding="utf-8",
    )
    staging_dir = staging_root / plan_id
    staging_dir.mkdir()
    (staging_dir / "memory.md").write_text("# old", encoding="utf-8")

    gc = MemoryGuardGc(workspace)
    plan = gc.plan(dry_run=False)
    paths = {item.path for item in plan.items}
    assert str(plan_file) in paths
    assert str(staging_dir) in paths

    result = gc.apply(plan, confirmed=True)
    assert result["ok"] is True
    assert not plan_file.exists()
    assert not staging_dir.exists()


def test_gc_apply_refuses_without_confirmed(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gc = MemoryGuardGc(workspace)
    plan = gc.plan(dry_run=False)
    result = gc.apply(plan, confirmed=False)
    assert result["ok"] is False


def test_gc_apply_refuses_paths_outside_memoryguard(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not delete", encoding="utf-8")
    gc = MemoryGuardGc(workspace)
    plan = gc.plan(dry_run=False)
    plan.items.append(GcPlanItem(
        path=str(outside),
        action="delete_file",
        reason="injected outside path",
        bytes_estimate=outside.stat().st_size,
        reversible=False,
    ))
    result = gc.apply(plan, confirmed=True)
    assert result["ok"] is False
    assert outside.exists()
    failed = [r for r in result["results"] if not r.get("ok")]
    assert any(r.get("error") == "path outside .memoryguard" for r in failed)


def test_governance_api_plan_and_apply(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    mg = _mg(workspace)
    _make_native_release(
        mg,
        "nrel-api",
        status="failed_verify",
        created_at=_old_iso(40),
    )

    api = _active_gui_api(workspace)
    plan = api.plan_memoryguard_gc(older_than_days=30)
    assert plan["dry_run"] is True
    assert plan["plan"]["blocked"] is False
    assert isinstance(plan["plan"]["candidate_count"], int)

    denied = api.apply_memoryguard_gc(confirmed=False)
    assert denied["code"] == "maintenance_confirmation_required"

    applied = api.apply_memoryguard_gc(confirmed=True, older_than_days=30)
    assert applied["ok"] is True
    assert applied["accepted"] is True
    assert applied["deferred"] is True
    assert applied["job_id"]
