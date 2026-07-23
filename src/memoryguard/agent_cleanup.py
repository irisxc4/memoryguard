"""v3.2 Agent 残留清理：标记 / 归档 / 恢复 / 历史。

安全边界：
- 不提供删除按钮
- 归档是移动到 .memoryguard/cleanup/archived-agents/，可恢复
- 标记卸载只是写入 uninstalled.json，扫描时跳过
- 所有操作写入 ledger.jsonl
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema_v3 import stable_hash


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class AgentCleanup:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / ".memoryguard" / "cleanup"
        self.ledger_path = self.root / "ledger.jsonl"
        self.uninstalled_path = self.root / "uninstalled.json"
        self.archived_dir = self.root / "archived-agents"

    def _ensure_dirs(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.archived_dir.mkdir(parents=True, exist_ok=True)

    def mark_uninstalled(self, candidate_id: str, product: str = "",
                         dir_path: str = "", reason: str = "") -> dict[str, Any]:
        """标记候选为已卸载。后续扫描会跳过。

        以 candidate_id 为主键。products 列表仅用于一次性旧数据迁移，不再写入。
        """
        self._ensure_dirs()
        data = {"products": [], "candidates": []}
        if self.uninstalled_path.exists():
            try:
                data = json.loads(self.uninstalled_path.read_text(encoding="utf-8"))
            except ValueError:
                data = {"products": [], "candidates": []}
        candidates = data.get("candidates", [])
        if not any(c.get("candidate_id") == candidate_id for c in candidates):
            candidates.append({
                "candidate_id": candidate_id,
                "product": product,
                "dir_path": dir_path,
            })
        data["candidates"] = candidates
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.uninstalled_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._append_ledger("mark_uninstalled", product, dir_path, reason, {
            "candidate_id": candidate_id,
        })
        return {"ok": True, "candidate_id": candidate_id, "marked_uninstalled": True}

    def unmark_uninstalled(self, candidate_id: str, product: str = "") -> dict[str, Any]:
        """取消已卸载标记。

        以 candidate_id 为主键移除。products 列表不再维护。
        """
        self._ensure_dirs()
        if not self.uninstalled_path.exists():
            return {"ok": True, "candidate_id": candidate_id, "marked_uninstalled": False}
        data = json.loads(self.uninstalled_path.read_text(encoding="utf-8"))
        candidates = data.get("candidates", [])
        candidates = [c for c in candidates if c.get("candidate_id") != candidate_id]
        data["candidates"] = candidates
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.uninstalled_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._append_ledger("unmark_uninstalled", product, "", "", {
            "candidate_id": candidate_id,
        })
        return {"ok": True, "candidate_id": candidate_id, "marked_uninstalled": False}

    def _load_uninstalled_candidates(self) -> set[str]:
        """读取已标记卸载的 candidate_id 集合。"""
        if not self.uninstalled_path.exists():
            return set()
        try:
            data = json.loads(self.uninstalled_path.read_text(encoding="utf-8"))
        except ValueError:
            return set()
        candidates = data.get("candidates", [])
        return {c["candidate_id"] for c in candidates if c.get("candidate_id")}

    def _load_uninstalled_set(self) -> set[str]:
        """[Deprecated] 返回已标记卸载的产品名集合，向后兼容用。

        新代码应使用 _load_uninstalled_candidates()。
        """
        if not self.uninstalled_path.exists():
            return set()
        try:
            data = json.loads(self.uninstalled_path.read_text(encoding="utf-8"))
        except ValueError:
            return set()
        return set(data.get("products", []))

    def _validate_archive_path(self, src: Path,
                               allowed_data_paths: list[str] | None = None) -> list[str]:
        """校验归档路径安全性，返回 reason_codes 列表（空表示通过）。"""
        reason_codes: list[str] = []
        try:
            src_resolved = src.resolve()
        except (OSError, RuntimeError):
            reason_codes.append("path_unresolvable")
            return reason_codes

        # 禁止归档用户主目录
        try:
            home = Path.home().resolve()
            if src_resolved == home:
                reason_codes.append("is_user_home")
            # Agent 私有数据根应在用户主目录下
            if home not in src_resolved.parents:
                reason_codes.append("outside_user_home")
        except (OSError, RuntimeError):
            reason_codes.append("home_check_failed")

        # 禁止归档 workspace 根目录
        if src_resolved == self.workspace:
            reason_codes.append("is_workspace_root")

        # 禁止归档 .memoryguard 内部目录（外部知识库）
        try:
            if src_resolved == self.root or self.root in src_resolved.parents:
                reason_codes.append("is_memoryguard_internal")
        except (OSError, RuntimeError):
            pass

        # 禁止归档符号链接或包含符号链接的路径
        try:
            if src.is_symlink():
                reason_codes.append("is_symlink")
            for parent in src.parents:
                if parent.is_symlink():
                    reason_codes.append("contains_symlink_in_path")
                    break
        except (OSError, RuntimeError):
            reason_codes.append("symlink_check_failed")

        # v3.2 P1: 路径必须属于 Profile 声明的私有数据根
        if allowed_data_paths is not None:
            src_resolved_str = str(src.resolve()).replace("\\", "/")
            allowed_resolved = [str(Path(p).resolve()).replace("\\", "/")
                                for p in allowed_data_paths if Path(p).exists()]
            if src_resolved_str not in allowed_resolved:
                reason_codes.append("path_not_in_profile_whitelist")
        else:
            reason_codes.append("no_profile_whitelist_provided")

        return reason_codes

    def archive_agent_dir(self, candidate_id: str, product: str, dir_path: str,
                          reason: str = "", dry_run: bool = False,
                          allowed_data_paths: list[str] | None = None) -> dict[str, Any]:
        """归档 Agent 目录：移动到 .memoryguard/cleanup/archived-agents/。

        可恢复。不删除。
        dry_run=True 时只返回预览信息不执行。
        allowed_data_paths: Profile 声明的私有数据根白名单，用于路径校验。
        """
        self._ensure_dirs()
        src = Path(dir_path)
        if not src.exists():
            return {"error": f"dir not found: {dir_path}"}

        reason_codes = self._validate_archive_path(src, allowed_data_paths=allowed_data_paths)
        if reason_codes:
            return {
                "error": "path_validation_failed",
                "reason_codes": reason_codes,
                "dir_path": str(src),
            }

        archive_id = stable_hash("archive", product, str(src),
                                 datetime.now(timezone.utc).isoformat())
        dest = self.archived_dir / archive_id / src.name

        preview = {
            "archive_id": archive_id,
            "candidate_id": candidate_id,
            "product": product,
            "original_path": str(src),
            "archived_path": str(dest),
            "reason": reason,
            "reason_codes": reason_codes,
        }

        if dry_run:
            return {"ok": True, "dry_run": True, "preview": preview}

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        manifest = {
            "archive_id": archive_id,
            "candidate_id": candidate_id,
            "product": product,
            "original_path": str(src),
            "archived_path": str(dest),
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
        }
        manifest_path = self.archived_dir / archive_id / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._append_ledger("archive", product, str(src), reason, {
            "archive_id": archive_id,
            "candidate_id": candidate_id,
            "archived_path": str(dest),
        })
        return {"ok": True, "archive_id": archive_id, "manifest": manifest}

    def restore_archived(self, archive_id: str) -> dict[str, Any]:
        """从归档恢复 Agent 目录到原位置。"""
        archive_root = self.archived_dir / archive_id
        manifest_path = archive_root / "manifest.json"
        if not manifest_path.exists():
            return {"error": f"archive not found: {archive_id}"}
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        archived_path = Path(manifest["archived_path"])
        original_path = Path(manifest["original_path"])
        if original_path.exists():
            return {"error": f"original path already exists: {original_path}"}
        original_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(archived_path), str(original_path))
        manifest["restored_at"] = datetime.now(timezone.utc).isoformat()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._append_ledger("restore", manifest["product"], str(original_path), "", {
            "archive_id": archive_id,
        })
        return {"ok": True, "archive_id": archive_id, "restored_to": str(original_path)}

    def purge_agent_dir(self, candidate_id: str, product: str, dir_path: str,
                        dry_run: bool = False,
                        allowed_data_paths: list[str] | None = None) -> dict[str, Any]:
        """直接清除 Agent 数据目录（不可恢复，不经过归档）。"""
        self._ensure_dirs()
        src = Path(dir_path)
        if not src.exists():
            return {"error": f"dir not found: {dir_path}"}

        reason_codes = self._validate_archive_path(src, allowed_data_paths=allowed_data_paths)
        if reason_codes:
            return {
                "error": "path_validation_failed",
                "reason_codes": reason_codes,
                "dir_path": str(src),
            }

        if dry_run:
            return {"ok": True, "dry_run": True, "preview": {
                "candidate_id": candidate_id,
                "product": product,
                "path": str(src),
                "action": "purge",
            }}

        tombstone = src.with_name(
            f".{src.name}.memoryguard-purge-"
            f"{stable_hash(candidate_id, str(src), datetime.now(timezone.utc).isoformat())}"
        )
        try:
            # 同盘原子改名先把目标从 Agent 的固定路径隔离，避免边删边写。
            src.rename(tombstone)
        except OSError as exc:
            permission_denied = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 5
            detail = {
                "candidate_id": candidate_id,
                "error_type": type(exc).__name__,
                "phase": "detach",
                "permission_denied": permission_denied,
            }
            if permission_denied and src.is_dir():
                fallback = self._purge_directory_contents(src, candidate_id, product, str(exc))
                if fallback.get("ok"):
                    return fallback
                detail["content_fallback"] = fallback
            self._append_ledger("purge_failed", product, str(src), str(exc), detail)
            if permission_denied:
                return {
                    "error": "purge_permission_denied",
                    "reason": "Windows 拒绝重命名该目录；内容级清理也未完全成功。请关闭占用该目录的 IDE/Agent 后重试。",
                    "dir_path": str(src),
                    "winerror": getattr(exc, "winerror", None),
                    "content_fallback": detail.get("content_fallback"),
                }
            return {
                "error": "purge_blocked",
                "reason": f"无法隔离目标目录，可能仍被程序占用：{exc}",
                "dir_path": str(src),
            }

        try:
            if tombstone.is_file():
                tombstone.unlink()
            else:
                shutil.rmtree(str(tombstone))
            if tombstone.exists():
                raise OSError("isolated path still exists after deletion")
        except OSError as exc:
            restored = False
            if tombstone.exists() and not src.exists():
                try:
                    tombstone.rename(src)
                    restored = True
                except OSError:
                    pass
            self._append_ledger("purge_failed", product, str(src), str(exc), {
                "candidate_id": candidate_id,
                "error_type": type(exc).__name__,
                "phase": "delete_isolated",
                "restored_original_path": restored,
            })
            return {
                "error": "purge_failed",
                "reason": f"隔离目录删除失败，请关闭相关程序后重试：{exc}",
                "dir_path": str(src),
                "restored_original_path": restored,
            }

        if src.exists():
            self._append_ledger("purge_recreated", product, str(src), "path_recreated", {
                "candidate_id": candidate_id,
            })
            return {
                "error": "purge_recreated",
                "reason": "目录已成功清除，但被后台程序立即重新创建。请完全退出对应 Agent、IDE 及其托盘进程后再试。",
                "dir_path": str(src),
            }
        self._append_ledger("purge", product, str(src), "", {
            "candidate_id": candidate_id,
        })
        return {"ok": True, "purged_path": str(src)}

    def _purge_directory_contents(self, src: Path, candidate_id: str, product: str,
                                  detach_error: str) -> dict[str, Any]:
        """根目录无法改名时，清空目录内容并保留根目录。"""
        deleted: list[str] = []
        blocked: list[dict[str, str]] = []
        try:
            children = list(src.iterdir())
        except OSError as exc:
            return {
                "error": "purge_contents_list_failed",
                "reason": str(exc),
                "dir_path": str(src),
            }

        for child in children:
            tombstone = child.with_name(
                f".{child.name}.memoryguard-purge-"
                f"{stable_hash(candidate_id, str(child), datetime.now(timezone.utc).isoformat())}"
            )
            try:
                child.rename(tombstone)
                if tombstone.is_file() or tombstone.is_symlink():
                    tombstone.unlink()
                else:
                    shutil.rmtree(str(tombstone))
                if child.exists() or tombstone.exists():
                    raise OSError("path still exists after deletion")
                deleted.append(str(child))
            except OSError as exc:
                direct_delete_error = None
                try:
                    if child.is_file() or child.is_symlink():
                        child.unlink()
                    else:
                        shutil.rmtree(str(child))
                    if child.exists():
                        raise OSError("path still exists after direct deletion")
                    deleted.append(str(child))
                    continue
                except OSError as delete_exc:
                    direct_delete_error = delete_exc
                external_delete = self._external_delete_child(child)
                if external_delete.get("ok") and not child.exists():
                    deleted.append(str(child))
                    continue
                blocked.append({
                    "path": str(child),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "winerror": str(getattr(exc, "winerror", "")),
                    "direct_delete_error": str(direct_delete_error) if direct_delete_error else "",
                    "direct_delete_error_type": type(direct_delete_error).__name__ if direct_delete_error else "",
                    "direct_delete_winerror": str(getattr(direct_delete_error, "winerror", "")) if direct_delete_error else "",
                    "external_delete": external_delete,
                })
                if tombstone.exists() and not child.exists():
                    try:
                        tombstone.rename(child)
                    except OSError:
                        pass

        if blocked:
            self._append_ledger("purge_contents_failed", product, str(src), detach_error, {
                "candidate_id": candidate_id,
                "deleted": deleted,
                "blocked": blocked,
                "root_preserved": True,
            })
            return {
                "error": "purge_contents_partial",
                "reason": "根目录无法重命名，且部分内容无法清除。请关闭占用这些文件的程序后重试。",
                "dir_path": str(src),
                "deleted": deleted,
                "blocked": blocked,
                "root_preserved": True,
            }

        self._append_ledger("purge_contents", product, str(src), detach_error, {
            "candidate_id": candidate_id,
            "deleted": deleted,
            "root_preserved": True,
        })
        return {
            "ok": True,
            "purged_path": str(src),
            "root_preserved": True,
            "deleted": deleted,
        }

    def _external_delete_child(self, child: Path) -> dict[str, Any]:
        if not bool(int(os.environ.get("MEMORYGUARD_ENABLE_EXTERNAL_DELETE", "0"))):
            return {"ok": False, "error": "external_delete_disabled"}
        if sys.platform != "win32":
            return {"ok": False, "error": "external_delete_not_supported"}
        try:
            return self._external_delete_child_windows(child)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}

    def _external_delete_child_windows(self, child: Path) -> dict[str, Any]:
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not powershell:
            return {"ok": False, "error": "powershell_not_found"}
        self._ensure_dirs()
        token = stable_hash(str(child), datetime.now(timezone.utc).isoformat())
        script_path = self.root / f"external-delete-{token}.ps1"
        result_path = self.root / f"external-delete-{token}.json"
        path_text = str(child)
        result_text = str(result_path)
        script = f"""
$ErrorActionPreference = 'Stop'
$path = {_ps_quote(path_text)}
$result = {_ps_quote(result_text)}
try {{
  if (Test-Path -LiteralPath $path) {{
    Remove-Item -LiteralPath $path -Recurse -Force
  }}
  $exists = Test-Path -LiteralPath $path
  @{{ ok = (-not $exists); exists = $exists; path = $path }} | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath $result
}} catch {{
  @{{ ok = $false; exists = (Test-Path -LiteralPath $path); path = $path; error = $_.Exception.Message; error_type = $_.Exception.GetType().FullName }} | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath $result
}}
"""
        script_path.write_text(script, encoding="utf-8")
        completed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "Start-Process -FilePath powershell.exe -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',"
                + _ps_quote(str(script_path))
                + ") -WindowStyle Hidden",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode != 0:
            return {"ok": False, "error": completed.stderr.strip() or f"launcher exited {completed.returncode}"}
        deadline = time.time() + 10
        while time.time() < deadline:
            if result_path.exists():
                try:
                    return json.loads(result_path.read_text(encoding="utf-8-sig"))
                except Exception as exc:
                    return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
            time.sleep(0.25)
        return {"ok": False, "error": "external_delete_timeout", "path": path_text}

    def delete_archived(self, archive_id: str) -> dict[str, Any]:
        archive_root = self.archived_dir / archive_id
        manifest_path = archive_root / "manifest.json"
        if not manifest_path.exists():
            return {"error": f"archive not found: {archive_id}"}
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        try:
            if archive_root.exists():
                shutil.rmtree(str(archive_root))
        except OSError as exc:
            self._append_ledger(
                "permanent_delete_failed",
                manifest.get("product", ""),
                manifest.get("original_path", ""),
                str(exc),
                {"archive_id": archive_id, "error_type": type(exc).__name__},
            )
            return {"error": "archive_delete_failed", "reason": str(exc), "archive_id": archive_id}
        if archive_root.exists():
            return {
                "error": "archive_delete_incomplete",
                "reason": "删除操作结束后归档目录仍然存在，请关闭占用该目录的程序后重试。",
                "archive_id": archive_id,
            }
        self._append_ledger("permanent_delete", manifest.get("product", ""), manifest.get("original_path", ""), "", {
            "archive_id": archive_id,
        })
        return {"ok": True, "archive_id": archive_id}

    def list_archives(self) -> list[dict[str, Any]]:
        """列出所有归档。"""
        if not self.archived_dir.exists():
            return []
        archives = []
        for sub in self.archived_dir.iterdir():
            if not sub.is_dir():
                continue
            manifest_path = sub / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                archives.append(manifest)
            except ValueError:
                continue
        return archives

    def list_cleanup_history(self) -> list[dict[str, Any]]:
        """读取 cleanup ledger。"""
        if not self.ledger_path.exists():
            return []
        history = []
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                history.append(json.loads(line))
            except ValueError:
                continue
        return history

    def _append_ledger(self, action: str, product: str, dir_path: str,
                       reason: str, detail: dict[str, Any]) -> None:
        self._ensure_dirs()
        event = {
            "event_id": stable_hash("cleanup_event", action, product, dir_path,
                                     datetime.now(timezone.utc).isoformat()),
            "action": action,
            "product": product,
            "dir_path": dir_path,
            "reason": reason,
            "detail": dict(detail),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.ledger_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
