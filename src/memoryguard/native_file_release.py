from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema_v3 import _now_iso, stable_hash


@dataclass
class NativeFileReleaseResult:
    ok: bool
    release_id: str
    status: str
    manifest_path: str
    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "release_id": self.release_id,
            "status": self.status,
            "manifest_path": self.manifest_path,
            "errors": list(self.errors),
        }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class SafeNativeFilePublisher:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / ".memoryguard" / "native_releases"
        self.root.mkdir(parents=True, exist_ok=True)

    def _ensure_parent_writable(self, target: Path) -> None:
        probe = target.parent / f".{target.name}.memoryguard-write-test.tmp"
        try:
            probe.write_bytes(b"test")
            probe.unlink()
        except OSError as exc:
            raise PermissionError(f"目标文件夹不可写：{target.parent}") from exc

    def apply(self, replacements: dict[str | Path, bytes], *, label: str = "native-memory") -> NativeFileReleaseResult:
        if not replacements:
            raise ValueError("replacements cannot be empty")
        release_id = "nrel-" + stable_hash(_now_iso(), label, *[str(Path(p).resolve()) for p in replacements])
        release_dir = self.root / release_id
        backup_dir = release_dir / "backup"
        staged_dir = release_dir / "staged"
        release_dir.mkdir(parents=True, exist_ok=False)
        backup_dir.mkdir(parents=True, exist_ok=True)
        staged_dir.mkdir(parents=True, exist_ok=True)
        manifest: dict[str, Any] = {
            "release_id": release_id,
            "label": label,
            "created_at": _now_iso(),
            "status": "preparing",
            "files": [],
        }
        errors: list[str] = []
        try:
            for index, (target_raw, content) in enumerate(replacements.items()):
                target = Path(target_raw).resolve()
                if target.exists() and not target.is_file():
                    raise ValueError(f"target is not a file: {target}")
                target.parent.mkdir(parents=True, exist_ok=True)
                self._ensure_parent_writable(target)
                existed_before = target.exists()
                before_hash = sha256_file(target) if existed_before else ""
                before_size = target.stat().st_size if existed_before else 0
                backup_path = backup_dir / f"{index}-{target.name}.bak"
                if existed_before:
                    shutil.copy2(target, backup_path)
                staged_path = staged_dir / f"{index}-{target.name}"
                staged_path.write_bytes(content)
                after_hash = sha256_bytes(content)
                if sha256_file(staged_path) != after_hash:
                    raise ValueError(f"staged checksum mismatch: {target}")
                manifest["files"].append({
                    "target_path": str(target),
                    "existed_before": existed_before,
                    "before_hash": before_hash,
                    "before_size": before_size,
                    "backup_path": str(backup_path) if existed_before else "",
                    "staged_path": str(staged_path),
                    "staged_hash": after_hash,
                    "after_hash": after_hash,
                    "applied": False,
                })
            self._write_manifest(release_dir, manifest)
            manifest["status"] = "applying"
            self._write_manifest(release_dir, manifest)
            applied: list[dict[str, Any]] = []
            try:
                for item in manifest["files"]:
                    target = Path(item["target_path"])
                    if item["existed_before"]:
                        current_hash = sha256_file(target)
                        if current_hash != item["before_hash"]:
                            raise RuntimeError(f"target changed before apply: {target}")
                    tmp = target.with_name(target.name + f".{release_id}.tmp")
                    shutil.copy2(item["staged_path"], tmp)
                    os.replace(tmp, target)
                    actual_hash = sha256_file(target)
                    if actual_hash != item["after_hash"]:
                        raise RuntimeError(f"target checksum mismatch after apply: {target}")
                    item["applied"] = True
                    applied.append(item)
                manifest["status"] = "applied_verified"
                self._write_manifest(release_dir, manifest)
                return NativeFileReleaseResult(True, release_id, manifest["status"], str(release_dir / "manifest.json"), [])
            except Exception as exc:
                errors.append(str(exc))
                self._restore_applied(applied)
                manifest["status"] = "failed_rolled_back"
                manifest["errors"] = errors
                self._write_manifest(release_dir, manifest)
                return NativeFileReleaseResult(False, release_id, manifest["status"], str(release_dir / "manifest.json"), errors)
        except Exception as exc:
            errors.append(str(exc))
            manifest["status"] = "failed_before_apply"
            manifest["errors"] = errors
            self._write_manifest(release_dir, manifest)
            return NativeFileReleaseResult(False, release_id, manifest["status"], str(release_dir / "manifest.json"), errors)

    def rollback(self, release_id: str, *, force: bool = False) -> NativeFileReleaseResult:
        release_dir = self.root / release_id
        manifest_path = release_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"release not found: {release_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        errors: list[str] = []
        try:
            for item in manifest.get("files", []):
                target = Path(item["target_path"])
                if target.exists():
                    current_hash = sha256_file(target)
                    if current_hash != item.get("after_hash") and not force:
                        raise RuntimeError(f"target changed after release: {target}")
                if item.get("existed_before"):
                    backup = Path(item["backup_path"])
                    if not backup.exists():
                        raise RuntimeError(f"backup missing: {backup}")
                    tmp = target.with_name(target.name + f".{release_id}.rollback.tmp")
                    shutil.copy2(backup, tmp)
                    os.replace(tmp, target)
                    if sha256_file(target) != item.get("before_hash"):
                        raise RuntimeError(f"rollback checksum mismatch: {target}")
                else:
                    if target.exists():
                        target.unlink()
            manifest["status"] = "rolled_back"
            manifest["rolled_back_at"] = _now_iso()
            self._write_manifest(release_dir, manifest)
            return NativeFileReleaseResult(True, release_id, "rolled_back", str(manifest_path), [])
        except Exception as exc:
            errors.append(str(exc))
            manifest["rollback_errors"] = errors
            self._write_manifest(release_dir, manifest)
            return NativeFileReleaseResult(False, release_id, manifest.get("status", "rollback_failed"), str(manifest_path), errors)

    def list_releases(self) -> list[dict[str, Any]]:
        releases: list[dict[str, Any]] = []
        for manifest_path in sorted(self.root.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                rollback_state = self._rollback_state(manifest)
                releases.append({
                    "release_id": manifest.get("release_id", manifest_path.parent.name),
                    "label": manifest.get("label", ""),
                    "created_at": manifest.get("created_at", ""),
                    "status": manifest.get("status", ""),
                    "file_count": len(manifest.get("files", [])),
                    "targets": [item.get("target_path", "") for item in manifest.get("files", [])],
                    "can_rollback": rollback_state["can_rollback"],
                    "rollback_reason": rollback_state["reason"],
                })
            except Exception:
                continue
        return releases

    def _rollback_state(self, manifest: dict[str, Any]) -> dict[str, Any]:
        files = manifest.get("files", [])
        if not files:
            return {"can_rollback": False, "reason": "没有可恢复文件"}
        status = manifest.get("status", "")
        for item in files:
            target = Path(item.get("target_path", ""))
            if not target.exists():
                if status == "rolled_back":
                    return {"can_rollback": False, "reason": "已经恢复过"}
                return {"can_rollback": False, "reason": "目标文件不存在"}
            if item.get("existed_before") and not Path(item.get("backup_path", "")).exists():
                return {"can_rollback": False, "reason": "备份缺失"}
            try:
                if sha256_file(target) != item.get("after_hash"):
                    if status == "rolled_back":
                        return {"can_rollback": False, "reason": "已经恢复过"}
                    if str(status).startswith("failed"):
                        return {"can_rollback": False, "reason": "发布未成功"}
                    return {"can_rollback": False, "reason": "目标已被后续修改"}
            except OSError:
                return {"can_rollback": False, "reason": "无法读取目标文件"}
        return {"can_rollback": True, "reason": "可恢复"}

    def get_manifest(self, release_id: str) -> dict[str, Any]:
        manifest_path = self.root / release_id / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"release not found: {release_id}")
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    def _write_manifest(self, release_dir: Path, manifest: dict[str, Any]) -> None:
        path = release_dir / "manifest.json"
        tmp = release_dir / "manifest.json.tmp"
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _restore_applied(self, items: list[dict[str, Any]]) -> None:
        for item in reversed(items):
            target = Path(item["target_path"])
            if item.get("existed_before"):
                backup = Path(item["backup_path"])
                if backup.exists():
                    tmp = target.with_name(target.name + ".restore.tmp")
                    shutil.copy2(backup, tmp)
                    os.replace(tmp, target)
            elif target.exists():
                target.unlink()
