"""`.memoryguard/` garbage collection with reconstructable-first retention.

Default behaviour is dry-run: callers must explicitly confirm before deletion.
Never touches ``cleanup/archived-agents/``, ``ir/current.json``, or ``ir/distilled.json``.
"""

from __future__ import annotations

import gzip
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .schema_v3 import _now_iso

MG_DIR = ".memoryguard"
GC_HISTORY_DIR = f"{MG_DIR}/gc-history"

PLAN_RETENTION_DAYS = 7
DECISIONS_ROTATE_BYTES = 10 * 1024 * 1024

_TERMINAL_NATIVE_STATUSES = frozenset({"applied_verified", "rolled_back"})
_FAILED_NATIVE_PREFIX = "failed_"


@dataclass
class GcPlanItem:
    path: str
    action: str  # delete_dir | delete_file | strip_backup_staged | rotate_jsonl
    reason: str
    bytes_estimate: int
    reversible: bool


@dataclass
class GcPlan:
    items: list[GcPlanItem]
    total_bytes: int
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [
                {
                    "path": item.path,
                    "action": item.action,
                    "reason": item.reason,
                    "bytes_estimate": item.bytes_estimate,
                    "reversible": item.reversible,
                }
                for item in self.items
            ],
            "total_bytes": self.total_bytes,
            "dry_run": self.dry_run,
        }


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        normalized = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _path_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                try:
                    total += child.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _is_protected_path(path: Path, mg_dir: Path) -> bool:
    try:
        rel = path.resolve().relative_to(mg_dir.resolve())
    except ValueError:
        return False
    parts = rel.parts
    if not parts:
        return False
    if parts[0] == "cleanup" and len(parts) >= 2 and parts[1] == "archived-agents":
        return True
    if parts[0] == "ir" and len(parts) >= 2 and parts[1] in ("current.json", "distilled.json"):
        return True
    return False


def _native_status_eligible(status: str) -> bool:
    if status in _TERMINAL_NATIVE_STATUSES:
        return True
    return status.startswith(_FAILED_NATIVE_PREFIX)


def _is_under_mg_dir(path: Path, mg_dir: Path) -> bool:
    try:
        path.resolve().relative_to(mg_dir.resolve())
        return True
    except ValueError:
        return False


class MemoryGuardGc:
    def __init__(
        self,
        workspace: str | Path,
        *,
        older_than_days: int = 30,
        keep_releases: int = 20,
        keep_snapshots: int = 3,
        keep_native_manifests: bool = True,
        decisions_rotate_bytes: int = DECISIONS_ROTATE_BYTES,
        plan_retention_days: int = PLAN_RETENTION_DAYS,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.mg_dir = self.workspace / MG_DIR
        self.older_than_days = older_than_days
        self.keep_releases = keep_releases
        self.keep_snapshots = keep_snapshots
        self.keep_native_manifests = keep_native_manifests
        self.decisions_rotate_bytes = decisions_rotate_bytes
        self.plan_retention_days = plan_retention_days

    def plan(self, *, dry_run: bool = True) -> GcPlan:
        items: list[GcPlanItem] = []
        if not self.mg_dir.is_dir():
            return GcPlan(items=[], total_bytes=0, dry_run=dry_run)

        items.extend(self._plan_native_releases())
        items.extend(self._plan_release_manager_artifacts())
        items.extend(self._plan_snapshots())
        items.extend(self._plan_decisions_jsonl())

        total = sum(item.bytes_estimate for item in items)
        return GcPlan(items=items, total_bytes=total, dry_run=dry_run)

    def apply(self, plan: GcPlan, *, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            return {"ok": False, "error": "apply refused: confirmed=False"}
        if plan.dry_run:
            return {"ok": False, "error": "apply refused: plan is dry_run"}

        history_dir = self.workspace / GC_HISTORY_DIR
        history_dir.mkdir(parents=True, exist_ok=True)
        ts = _now_iso().replace(":", "-")
        history_path = history_dir / f"{ts}.json"
        history: dict[str, Any] = {
            "timestamp": _now_iso(),
            "dry_run": False,
            "plan": plan.to_dict(),
            "results": [],
        }
        history_path.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        results: list[dict[str, Any]] = []
        ok = True
        for item in plan.items:
            target = Path(item.path)
            if not _is_under_mg_dir(target, self.mg_dir):
                results.append({
                    "path": item.path,
                    "action": item.action,
                    "ok": False,
                    "error": "path outside .memoryguard",
                })
                ok = False
                continue
            if _is_protected_path(target, self.mg_dir):
                results.append({
                    "path": item.path,
                    "action": item.action,
                    "ok": False,
                    "error": "protected path",
                })
                ok = False
                continue
            try:
                result = self._apply_item(item)
                results.append(result)
                if not result.get("ok", False):
                    ok = False
            except Exception as exc:  # noqa: BLE001
                results.append({
                    "path": item.path,
                    "action": item.action,
                    "ok": False,
                    "error": str(exc),
                })
                ok = False

        history["results"] = results
        history["ok"] = ok
        history_path.write_text(
            json.dumps(history, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "ok": ok,
            "history_path": str(history_path),
            "applied": len([r for r in results if r.get("ok")]),
            "failed": len([r for r in results if not r.get("ok")]),
            "results": results,
        }

    def _cutoff(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(days=self.older_than_days)

    def _plan_cutoff(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(days=self.plan_retention_days)

    def _plan_native_releases(self) -> list[GcPlanItem]:
        items: list[GcPlanItem] = []
        releases_root = self.mg_dir / "native_releases"
        if not releases_root.is_dir():
            return items

        cutoff = self._cutoff()
        for release_dir in sorted(releases_root.iterdir()):
            if not release_dir.is_dir():
                continue
            manifest_path = release_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            status = str(manifest.get("status", ""))
            if not _native_status_eligible(status):
                continue
            created = _parse_iso(str(manifest.get("created_at", "")))
            if created is None or created > cutoff:
                continue

            for sub in ("backup", "staged"):
                subdir = release_dir / sub
                if not subdir.is_dir():
                    continue
                size = _path_bytes(subdir)
                items.append(GcPlanItem(
                    path=str(subdir),
                    action="delete_dir",
                    reason=(
                        f"native release {release_dir.name} status={status} "
                        f"older than {self.older_than_days}d; reconstructable copy"
                    ),
                    bytes_estimate=size,
                    reversible=True,
                ))
        return items

    def _plan_release_manager_artifacts(self) -> list[GcPlanItem]:
        items: list[GcPlanItem] = []
        plans_dir = self.mg_dir / "plans"
        staging_root = self.mg_dir / "staging"
        if plans_dir.is_dir():
            cutoff = self._plan_cutoff()
            for plan_file in sorted(plans_dir.glob("*.json")):
                plan_id = plan_file.stem
                created_at: datetime | None = None
                try:
                    data = json.loads(plan_file.read_text(encoding="utf-8"))
                    created_at = _parse_iso(str(data.get("created_at", "")))
                except (json.JSONDecodeError, OSError):
                    pass
                file_time = datetime.fromtimestamp(plan_file.stat().st_mtime, tz=timezone.utc)
                effective = created_at if created_at is not None else file_time
                if effective > cutoff:
                    continue

                size = _path_bytes(plan_file)
                items.append(GcPlanItem(
                    path=str(plan_file),
                    action="delete_file",
                    reason=(
                        f"build plan {plan_id} older than {self.plan_retention_days}d; "
                        "manifest embedded in release JSON"
                    ),
                    bytes_estimate=size,
                    reversible=True,
                ))

                staging_dir = staging_root / plan_id
                if staging_dir.is_dir():
                    size = _path_bytes(staging_dir)
                    items.append(GcPlanItem(
                        path=str(staging_dir),
                        action="delete_dir",
                        reason=f"staging for plan {plan_id} older than {self.plan_retention_days}d",
                        bytes_estimate=size,
                        reversible=True,
                    ))

        # P1.3: keep_releases + older_than_days 双条件裁剪 releases/*.json
        # 仅当 rank >= keep_releases AND age >= older_than_days 时才处理
        # 先归档精简审计摘要,校验成功后再删除 JSON;不可恢复删除标记 reversible=False
        releases_dir = self.mg_dir / "releases"
        if releases_dir.is_dir():
            release_files: list[tuple[datetime, Path, str, str, dict]] = []
            for rf in releases_dir.glob("*.json"):
                applied_at: datetime | None = None
                status = ""
                release_id = rf.stem
                rdata: dict = {}
                try:
                    rdata = json.loads(rf.read_text(encoding="utf-8"))
                    applied_at = _parse_iso(str(rdata.get("applied_at", "")))
                    status = str(rdata.get("status", ""))
                    release_id = rdata.get("release_id", release_id)
                except (json.JSONDecodeError, OSError):
                    # P1.3: 损坏 JSON 保留并报警,不删除
                    items.append(GcPlanItem(
                        path=str(rf),
                        action="delete_file",
                        reason=f"release {rf.stem} corrupt JSON; preserved for manual review",
                        bytes_estimate=_path_bytes(rf),
                        reversible=False,
                    ))
                    continue
                file_time = datetime.fromtimestamp(rf.stat().st_mtime, tz=timezone.utc)
                effective = applied_at if applied_at is not None else file_time
                release_files.append((effective, rf, release_id, status, rdata))
            # 按时间降序排序,rank 0 最新
            release_files.sort(key=lambda x: x[0], reverse=True)
            cutoff = self._cutoff()
            for rank, (ts, rf, release_id, status, rdata) in enumerate(release_files):
                # 双条件:rank >= keep_releases AND age >= older_than_days
                if rank < self.keep_releases:
                    continue
                if ts > cutoff:
                    # 未过期,不删除
                    continue
                # 归档精简审计摘要(先写摘要,校验成功后才计划删除)
                archive_ok = self._archive_release_summary(rf, release_id, status, rdata)
                if not archive_ok:
                    # 归档失败:不删除,保留原文件
                    items.append(GcPlanItem(
                        path=str(rf),
                        action="delete_file",
                        reason=f"release {release_id} archive failed; preserved",
                        bytes_estimate=_path_bytes(rf),
                        reversible=False,
                    ))
                    continue
                # 删除 JSON(不可恢复,摘要已归档)
                items.append(GcPlanItem(
                    path=str(rf),
                    action="delete_file",
                    reason=(
                        f"release {release_id} status={status or 'unknown'} "
                        f"rank={rank} >= keep_releases={self.keep_releases} "
                        f"AND age >= {self.older_than_days}d; summary archived"
                    ),
                    bytes_estimate=_path_bytes(rf),
                    reversible=False,
                ))
        return items

    def _archive_release_summary(self, release_path: Path, release_id: str,
                                  status: str, rdata: dict) -> bool:
        """归档精简审计摘要到 gc-history/releases-audit/。

        校验写入成功后才返回 True。摘要只含审计必要字段,不含完整 backup_paths。
        """
        audit_dir = self.workspace / GC_HISTORY_DIR / "releases-audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "release_id": release_id,
            "status": status,
            "applied_at": rdata.get("applied_at", ""),
            "build_id": rdata.get("build_id", ""),
            "target_profile": rdata.get("target_profile", ""),
            "changed_count": len(rdata.get("changed_paths", [])),
            "archived_at": _now_iso(),
        }
        summary_path = audit_dir / f"{release_id}.json"
        try:
            tmp = summary_path.with_name(summary_path.name + ".tmp")
            tmp.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            import os as _os
            _os.replace(tmp, summary_path)
            # 校验写入成功
            if not summary_path.exists():
                return False
            json.loads(summary_path.read_text(encoding="utf-8"))
            return True
        except (OSError, json.JSONDecodeError):
            return False

    def _plan_snapshots(self) -> list[GcPlanItem]:
        items: list[GcPlanItem] = []
        snapshots_root = self.mg_dir / "snapshots"
        if not snapshots_root.is_dir():
            return items

        dirs = [d for d in snapshots_root.iterdir() if d.is_dir()]
        dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for old_dir in dirs[self.keep_snapshots:]:
            size = _path_bytes(old_dir)
            items.append(GcPlanItem(
                path=str(old_dir),
                action="delete_dir",
                reason=f"snapshot exceeds keep_snapshots={self.keep_snapshots}",
                bytes_estimate=size,
                reversible=True,
            ))
        return items

    def _plan_decisions_jsonl(self) -> list[GcPlanItem]:
        items: list[GcPlanItem] = []
        decisions_path = self.mg_dir / "ir" / "decisions.jsonl"
        if not decisions_path.is_file():
            return items
        size = decisions_path.stat().st_size
        if size <= self.decisions_rotate_bytes:
            return items
        items.append(GcPlanItem(
            path=str(decisions_path),
            action="rotate_jsonl",
            reason=(
                f"decisions.jsonl size {size} exceeds "
                f"rotate threshold {self.decisions_rotate_bytes}"
            ),
            bytes_estimate=size,
            reversible=True,
        ))
        return items

    def _apply_item(self, item: GcPlanItem) -> dict[str, Any]:
        target = Path(item.path)
        if item.action == "delete_dir":
            if not target.exists():
                return {"path": item.path, "action": item.action, "ok": True, "skipped": True}
            if not target.is_dir():
                return {
                    "path": item.path,
                    "action": item.action,
                    "ok": False,
                    "error": "not a directory",
                }
            shutil.rmtree(target)
            return {"path": item.path, "action": item.action, "ok": True}

        if item.action == "delete_file":
            if not target.exists():
                return {"path": item.path, "action": item.action, "ok": True, "skipped": True}
            if not target.is_file():
                return {
                    "path": item.path,
                    "action": item.action,
                    "ok": False,
                    "error": "not a file",
                }
            target.unlink()
            return {"path": item.path, "action": item.action, "ok": True}

        if item.action == "rotate_jsonl":
            return self._rotate_decisions_jsonl(target)

        if item.action == "strip_backup_staged":
            removed: list[str] = []
            for sub in ("backup", "staged"):
                subdir = target / sub
                if subdir.is_dir():
                    shutil.rmtree(subdir)
                    removed.append(sub)
            return {
                "path": item.path,
                "action": item.action,
                "ok": True,
                "removed": removed,
            }

        return {
            "path": item.path,
            "action": item.action,
            "ok": False,
            "error": f"unknown action: {item.action}",
        }

    def _rotate_decisions_jsonl(self, decisions_path: Path) -> dict[str, Any]:
        if not decisions_path.is_file():
            return {
                "path": str(decisions_path),
                "action": "rotate_jsonl",
                "ok": True,
                "skipped": True,
            }
        ir_dir = decisions_path.parent
        ir_dir.mkdir(parents=True, exist_ok=True)
        ts = _now_iso().replace(":", "-")
        archive_name = f"decisions.{ts}.jsonl.gz"
        archive_path = ir_dir / archive_name
        if archive_path.exists():
            archive_path = ir_dir / f"decisions.{ts}.1.jsonl.gz"
        with decisions_path.open("rb") as src, gzip.open(archive_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        decisions_path.write_text("", encoding="utf-8")
        return {
            "path": str(decisions_path),
            "action": "rotate_jsonl",
            "ok": True,
            "archive_path": str(archive_path),
        }
